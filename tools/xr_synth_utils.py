import argparse
import os, sys
import glob
from pathlib import Path
import time
import numpy as np
import torch
from copy import copy
import open3d
from queue import Queue
from datetime import datetime as dt
import re
sys.path.insert(0, '../../OusterTesting')
import utils_ouster

def sorted_alphanumeric(data):
    """
    Sort the given iterable in the way that humans expect.
    Args:
        data: An iterable.
    Returns: sorted version of the given iterable.
    """
    convert = lambda text: int(text) if text.isdigit() else text.lower()
    alphanum_key = lambda key: [ convert(c) for c in re.split('([0-9]+)', key) ] 
    return sorted(data, key=alphanum_key)
def filter_predictions(pred_dict, classes_to_use):
    """
    Filter predictions to only include the classes we want to use.
    """
    if isinstance(pred_dict["pred_labels"],torch.Tensor):
        pred_dict["pred_labels"] = pred_dict["pred_labels"].cpu().numpy().astype(int)
    if isinstance(pred_dict["pred_boxes"],torch.Tensor):
        pred_dict["pred_boxes"] = pred_dict["pred_boxes"].cpu().numpy()
    if isinstance(pred_dict["pred_scores"],torch.Tensor):
        pred_dict["pred_scores"] = pred_dict["pred_scores"].cpu().numpy()
    if classes_to_use is not None and len(pred_dict["pred_labels"]) > 0:
        
        #indices = [np.nonzero(sum(pred_dict["pred_labels"]==x for x in classes_to_use))[0].tolist()][0]
        #print(np.nonzero((sum(pred_dict["pred_labels"]-1==x for x in classes_to_use))))
        indices = np.nonzero((sum(pred_dict["pred_labels"]-1==x for x in classes_to_use)))[0].tolist()
        #print(f"indices: {indices}")
        pred_dict["pred_boxes"] = pred_dict["pred_boxes"].reshape(pred_dict["pred_boxes"].shape[0],-1)[indices,:]
        pred_dict["pred_labels"] = pred_dict["pred_labels"].reshape(pred_dict["pred_labels"].shape[0],-1)[indices,:]-1
        pred_dict["pred_scores"] = pred_dict["pred_scores"].reshape(pred_dict["pred_scores"].shape[0],-1)[indices,:]
    elif len(pred_dict["pred_labels"]) > 0:
        pred_dict["pred_boxes"] = pred_dict["pred_boxes"].reshape(pred_dict["pred_boxes"].shape[0],-1)
        pred_dict["pred_labels"] = pred_dict["pred_labels"].reshape(pred_dict["pred_labels"].shape[0],-1)-1
        pred_dict["pred_scores"] = pred_dict["pred_scores"].reshape(pred_dict["pred_scores"].shape[0],-1)
    return pred_dict
    
def generate_distance_matrix(pred_dict):
    if isinstance(pred_dict["pred_boxes"],torch.Tensor):
        pred_dict["pred_boxes"] = pred_dict["pred_boxes"].cpu().numpy()
    pred_dict["distance_matrix"] = np.zeros((pred_dict["pred_boxes"].shape[0],pred_dict["pred_boxes"].shape[0]))
    for i in range(pred_dict["pred_boxes"].shape[0]):
        for j in range(pred_dict["pred_boxes"].shape[0]):
            pred_dict["distance_matrix"][i,j] = np.linalg.norm(pred_dict["pred_boxes"][i,:3]-pred_dict["pred_boxes"][j,:3])
    return pred_dict

def format_predictions(pred_dict):
    """
    Format predictions to be more readable.
    """
    if isinstance(pred_dict["pred_labels"],torch.Tensor):
        pred_dict["pred_labels"] = pred_dict["pred_labels"].cpu().numpy().astype(int)
    if isinstance(pred_dict["pred_boxes"],torch.Tensor):
        pred_dict["pred_boxes"] = pred_dict["pred_boxes"].cpu().numpy()
    if isinstance(pred_dict["pred_scores"],torch.Tensor):
        pred_dict["pred_scores"] = pred_dict["pred_scores"].cpu().numpy()
    if len(pred_dict["pred_labels"]) > 0:
        pred_dict["pred_labels"] = pred_dict["pred_labels"]-1
    return pred_dict
def display_predictions(pred_dict, class_names, logger=None):
    """
    Display predictions.
    args:
        pred_dict: prediction dictionary. "pred_boxes", "pred_labels", "pred_scores"
        class_names: list of class names
    """
    if logger is None:
        return
    logger.info(f"Model detected: {len(pred_dict['pred_labels'])} objects.")
    for lbls,score in zip(pred_dict['pred_labels'],pred_dict['pred_scores']):
        logger.info(f"lbls: {lbls} score: {score}")
        logger.info(f"\t Prediciton {class_names[lbls[0]]}, id: {lbls[0]} with confidence: {score[0]:.3e}.")
class CSVRecorder():
    """
    Class to record predictions and point clouds to a CSV file.
    """
    def __init__(self, 
                 folder_name=f"csv_folder_{dt.now().strftime('%Y%m%d_%H%M%S')}",
                 main_folder="./lidarCSV",
                 class_names=None,
                 ):
        self.main_folder = main_folder
        self.folder_name = folder_name
        self.class_names = class_names
        self.path = os.path.join(self.main_folder, self.folder_name)
        
        self.labelfile = "label"
        self.cloudfile = "cloud"
        if not os.path.exists(self.main_folder):
            os.makedirs(self.main_folder)
        if not os.path.exists(self.path):
            os.makedirs(self.path)
        self.frames = 0
    def process_labels(self,pred_dict):
        boxes = np.array(pred_dict["pred_boxes"][:,:9])
        labels = np.array([self.class_names[int(x)] for x in pred_dict["pred_labels"]] if len(pred_dict["pred_labels"]) > 0 else []).reshape(-1,1)
        scores = np.array(pred_dict["pred_scores"]).reshape(-1,1)
        #print(f"boxes: {boxes}")
        #print(f"labels: {labels}")
        #print(f"scores: {scores}")

        labels = np.concatenate((boxes,labels,pred_dict["pred_labels"].reshape(-1,1),scores),axis=1)
        return labels

    def add_frame_file(self, cloud,pred_dict):
        cloud_name = os.path.join(self.path, f"cloud_{self.frames}.csv")
        label_name = os.path.join(self.path, f"label_{self.frames}.csv")
        np.savetxt(cloud_name, cloud, header = "x, y, z, r",delimiter=",")
        np.savetxt(label_name, self.process_labels(pred_dict=pred_dict), header = "x, y, z, rotx, roty, roz, l, w, h, label, label_idx, score",delimiter=",",fmt="%s")
        self.frames += 1

class OusterStreamer():
    def __init__(self, stream):
        self.stream = stream
        self.started = False
        self.q_xyzr = Queue()

    def start_thread(self):
        self.started = True
        self.thread = threading.Thread(target=self.stream_loop)
        #self.thread.daemon = True
        self.thread.start()
        time.sleep(0.2)
    def stop_thread(self):
        self.started = False
        self.thread.join()
    def stream_loop(self):
        for scan in self.stream:
            if not self.started:
                break
            xyz = utils_ouster.get_xyz(self.stream,scan)
            signal = utils_ouster.get_signal_reflection(self.stream,scan)
            xyzr = utils_ouster.convert_to_xyzr(xyz,signal)
            self.q_xyzr.put(utils_ouster.compress_mid_dim(xyzr))
    def get_pcd(self):
        try:
            return self.q_xyzr.get(timeout=1e-6)
        except:
            return None
class TimeLogger():
    def __init__(self):
        super().__init__()
        self.time_dict = {}
        self.time_pd = None

    def create_metric(self, name: str):
        self.time_dict[name] = []
    
    def log_time(self, name: str, time: float):
        self.time_dict[name].append(time)
    
    def visualize_results(self):
        time_averages = {}
        for key in self.time_dict:
            
            plt.plot(range(0,len(self.time_dict [key])),self.time_dict[key])
            time_averages[key] = np.mean(self.time_dict[key])
            #plt.show()
        time_pd = pd.DataFrame(time_averages,index=[0])
        print(f"time_pd: {time_pd}")

if __name__ == "__main__":
    T = TimeLogger()
    data = {'a': np.random.rand(10), 'b': np.random.rand(10), 'c': np.random.rand(10)}
    T.create_metric('a')
    T.create_metric('b')
    T.create_metric('c')
    T.log_time('a', data['a'])
    T.log_time('b', data['b'])
    T.log_time('c', data['c'])
    T.visualize_results()