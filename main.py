import torch
import numpy as np
import os,cv2,time,torch,random,pytorchvideo,warnings,argparse,math
warnings.filterwarnings("ignore",category=UserWarning)

from pytorchvideo.transforms.functional import (
    uniform_temporal_subsample,
    short_side_scale_with_boxes,
    clip_boxes_to_image,)
from torchvision.transforms._functional_video import normalize
from pytorchvideo.data.ava import AvaLabeledVideoFramePaths
from pytorchvideo.models.hub import slowfast_r50_detection
from deep_sort.deep_sort import DeepSort

from selfutils import save_video,send_image
import threading
from os.path import join

class MyVideoCapture:
    
    def __init__(self, source):
        self.filename = source
        self.cap = cv2.VideoCapture(source)
        self.idx = -1
        self.end = False
        self.stack = []
        
    def read(self):
        self.idx += 1
        ret, img = self.cap.read()
        if ret:
            self.stack.append(img)
        else:
            self.end = True
        return ret, img
    
    def to_tensor(self, img):
        img = torch.from_numpy(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        return img.unsqueeze(0)
        
    def get_video_clip(self):
        assert len(self.stack) > 0, "clip length must large than 0 !"
        self.stack = [self.to_tensor(img) for img in self.stack]
        clip = torch.cat(self.stack).permute(-1, 0, 1, 2)
        del self.stack
        self.stack = []
        return clip
    
    def release(self):
        self.cap.release()

    def get_frames_around_index(self, index, frame_buffer):
        frames = []
        cap = cv2.VideoCapture(self.filename)

        if not cap.isOpened():
            print("Error: Unable to open video file.")
            return []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        for i in range(index - frame_buffer, index + frame_buffer + 1):
            if i < 0 or i >= total_frames:
                # Skip frames that are out of bounds
                continue

            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if ret:
                frames.append(frame)
            else:
                print(f"Error reading frame {i}")
        cap.release()
        return frames
            
def tensor_to_numpy(tensor):
    img = tensor.cpu().numpy().transpose((1, 2, 0))
    return img

def ava_inference_transform(
    clip, 
    boxes,
    num_frames = 32, #if using slowfast_r50_detection, change this to 32, 4 for slow 
    crop_size = 640, 
    data_mean = [0.45, 0.45, 0.45], 
    data_std = [0.225, 0.225, 0.225],
    slow_fast_alpha = 4, #if using slowfast_r50_detection, change this to 4, None for slow
):
    boxes = np.array(boxes)
    roi_boxes = boxes.copy()
    clip = uniform_temporal_subsample(clip, num_frames)
    clip = clip.float()
    clip = clip / 255.0
    height, width = clip.shape[2], clip.shape[3]
    boxes = clip_boxes_to_image(boxes, height, width)
    clip, boxes = short_side_scale_with_boxes(clip,size=crop_size,boxes=boxes,)
    clip = normalize(clip,
        np.array(data_mean, dtype=np.float32),
        np.array(data_std, dtype=np.float32),) 
    boxes = clip_boxes_to_image(boxes, clip.shape[2],  clip.shape[3])
    if slow_fast_alpha is not None:
        fast_pathway = clip
        slow_pathway = torch.index_select(clip,1,
            torch.linspace(0, clip.shape[1] - 1, clip.shape[1] // slow_fast_alpha).long())
        clip = [slow_pathway, fast_pathway]
    
    return clip, torch.from_numpy(boxes), roi_boxes

def plot_one_box(x, img, color=[100,100,100], text_info="None",
                 velocity=None, thickness=1, fontsize=0.5, fontthickness=1):
    # Plots one bounding box on image img
    c1, c2 = (int(x[0]), int(x[1])), (int(x[2]), int(x[3]))
    cv2.rectangle(img, c1, c2, color, thickness, lineType=cv2.LINE_AA)
    t_size = cv2.getTextSize(text_info, cv2.FONT_HERSHEY_TRIPLEX, fontsize , fontthickness+2)[0]
    cv2.rectangle(img, c1, (c1[0] + int(t_size[0]), c1[1] + int(t_size[1]*1.45)), color, -1)
    cv2.putText(img, text_info, (c1[0], c1[1]+t_size[1]+2), 
                cv2.FONT_HERSHEY_TRIPLEX, fontsize, [255,255,255], fontthickness)
    return img

def deepsort_update(Tracker, pred, xywh, np_img):
    outputs = Tracker.update(xywh, pred[:,4:5],pred[:,5].tolist(),cv2.cvtColor(np_img,cv2.COLOR_BGR2RGB))
    return outputs

def save_yolopreds_tovideo(yolo_preds, id_to_ava_labels, color_map, output_video, vis=False):
    for i, (im, pred) in enumerate(zip(yolo_preds.ims, yolo_preds.pred)):
        im=cv2.cvtColor(im,cv2.COLOR_BGR2RGB)
        if pred.shape[0]:
            for j, (*box, cls, trackid, vx, vy) in enumerate(pred):
                if int(cls) != 0:
                    ava_label = ''
                elif trackid in id_to_ava_labels.keys():
                    ava_label = id_to_ava_labels[trackid].split(' ')[0]
                else:
                    ava_label = 'Unknown'
                text = '{} {} {}'.format(int(trackid),yolo_preds.names[int(cls)],ava_label)
                color = color_map[int(cls)]
                im = plot_one_box(box,im,color,text)
        im = im.astype(np.uint8)
        im = cv2.cvtColor(im,cv2.COLOR_RGB2BGR)
        output_video.write(im)
        if vis:
            im=cv2.cvtColor(im,cv2.COLOR_RGB2BGR)
            cv2.imshow("demo", im)

def main(config):
    device = config.device
    imsize = config.imsize
    model = torch.hub.load('ultralytics/yolov5', 'custom', path='models/yolo_model.pt', force_reload=True) 
    model.conf = config.conf
    model.iou = config.iou
    model.max_det = 100
    flag = True
    if config.classes:
        model.classes = config.classes
    
    video_model = slowfast_r50_detection(True).eval().to(device)
    
    deepsort_tracker = DeepSort("models/ckpt.t7")
    ava_labelnames,_ = AvaLabeledVideoFramePaths.read_label_map("selfutils/temp.pbtxt")
    coco_color_map = [[random.randint(0, 255) for _ in range(3)] for _ in range(80)]

    vide_save_path = config.output
    video=cv2.VideoCapture(config.input)
    width,height = int(video.get(3)),int(video.get(4))
    video.release()
    outputvideo = cv2.VideoWriter(vide_save_path,cv2.VideoWriter_fourcc(*'mp4v'), 25, (width,height))
    print("processing...")
    
    cap = MyVideoCapture(config.input)
    id_to_ava_labels = {}
    a=time.time()
    while not cap.end:
        ret, img = cap.read()
        if not ret:
            print("ret false")
            continue
        yolo_preds=model([img], size=imsize)
        yolo_preds.files=["img.jpg"]
        
        deepsort_outputs=[]
        for j in range(len(yolo_preds.pred)):
            temp=deepsort_update(deepsort_tracker,yolo_preds.pred[j].cpu(),yolo_preds.xywh[j][:,0:4].cpu(),yolo_preds.ims[j])
            if len(temp)==0:
                temp=np.ones((0,8))
            deepsort_outputs.append(temp.astype(np.float32))
            
        yolo_preds.pred=deepsort_outputs
        def process():
            frames = cap.get_frames_around_index(index=cap.idx,frame_buffer=25)
            file_name = f"video_{cap.idx}.mp4"
            save_video(frame_list=frames,dst=os.path.join("tmp",file_name))
            resp = send_image(file_name=f"video_{cap.idx}.mp4")

            if resp == 200:
                print("send successfully")
            else:
                print("send unsuccessfully!")

            

        if len(cap.stack) == 50:
            print(f"processing {cap.idx // 50}th second clips")
            clip = cap.get_video_clip()
            if yolo_preds.pred[0].shape[0]:
                inputs, inp_boxes, _=ava_inference_transform(clip, yolo_preds.pred[0][:,0:4], crop_size=imsize)
                inp_boxes = torch.cat([torch.zeros(inp_boxes.shape[0],1), inp_boxes], dim=1)
                if isinstance(inputs, list):
                    inputs = [inp.unsqueeze(0).to(device) for inp in inputs]
                else:
                    inputs = inputs.unsqueeze(0).to(device)
                with torch.no_grad():
                    slowfaster_preds = video_model(inputs, inp_boxes.to(device))
                    slowfaster_preds = slowfaster_preds.cpu()

                slowfast_det = []
                for data in slowfaster_preds:
                  sorted_indices = np.argsort(data.tolist())[::-1]

                  # Get the top 5 values and their indices
                  top_5_values = [data[i] for i in sorted_indices[:5]]
                  top_5_indices = sorted_indices[:5]

                  slowfast_det.append(top_5_indices)
               
                for tid,labels in zip(yolo_preds.pred[0][:,5].tolist(), slowfast_det):
                    if 63 in labels and flag:
                      label_id = 63
                      print("Fight Detected!")
                      my_thread = threading.Thread(target=process)
                      my_thread.start()
                      flag = False
                    else:
                      label_id = labels[0]
                    id_to_ava_labels[tid] = ava_labelnames[label_id+1]
                flag = True
        save_yolopreds_tovideo(yolo_preds, id_to_ava_labels, coco_color_map, outputvideo, config.show)
    print("total cost: {:.3f} s, video length: {} s".format(time.time()-a, cap.idx / 25))
    
    cap.release()
    outputvideo.release()
    print('saved video to:', vide_save_path)
    
    
if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, default="input.mp4", help='test imgs folder or video or camera')
    parser.add_argument('--output', type=str, default="output.mp4", help='folder to save result imgs, can not use input folder')
    # object detect config
    parser.add_argument('--imsize', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf', type=float, default=0.4, help='object confidence threshold')
    parser.add_argument('--iou', type=float, default=0.4, help='IOU threshold for NMS')
    parser.add_argument('--device', default='cuda', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--show', action='store_true', help='show img')
    config = parser.parse_args()

    # pre requist 
    if not os.path.isfile("models/ckpt.t7"):
        os.makedirs("models",exist_ok=True)
        if not os.path.isfile("models/ckpt.t7"):
            raise Exception("ckpt.t7 file is missing!")
    if not os.path.isfile("models/yolo_model.pt"):
        os.makedirs("models",exist_ok=True)
        if not os.path.isfile("models/yolo_model.pt"):
            raise Exception("yolo_model.pt file is missing!")
    os.makedirs("tmp",exist_ok=True)
        
    if config.input.isdigit():
        print("using local camera.")
        config.input = int(config.input)
        
    print(config)
    main(config)
