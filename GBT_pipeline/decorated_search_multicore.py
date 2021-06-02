# ============================================================
# Author: Peter Xiangyuan Ma
# Date: May 19 2021
# Purpose: split the search functionality into smaller chuncks 
# to be called by the full_search.py pipeline. This code, loops 
# through chunks of the cadence and preprocesses it, 
# feed into neural network and then runs the clustering algorithm
# in parallel using multiple CPU cores. 
# ============================================================

import numpy as np
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
import sys
sys.path.insert(1, '../ML_Training')
from execute_model import model_predict_distribute
from preprocess import get_data
from numba import jit, prange, njit
from blimpy import Waterfall
import time
import random
from sklearn.cluster import SpectralClustering
import pandas as pd
import tensorflow as tf
from multiprocessing import Pool
import functools
import warnings
from tqdm import tqdm
from sklearn.metrics import silhouette_score

def sizeof_fmt(num, suffix='B'):
    ''' by Fred Cirera,  https://stackoverflow.com/a/1094933/1870254, modified'''
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'Yi', suffix)


def screening(data, labels, index):
    metric = [0.9,0.9,0.9,0.9,0.9,0.9,0.9,
              0.9,0.9,0.9,0.9,0.9,0.9,0.9 ]
    fit = silhouette_score(data,labels)
    if fit < metric[index]:
        return False, fit
    return True, fit

# Function takes in small distributed chunks of data and runs spectral clustering on the data set
# returns a list of candidates with the frequency range. 
def compute_parallel(result, cadence_length,WINDOW_SIZE,index,freq_ranges, n):
    # spectral clustering
    labels = SpectralClustering(n_clusters=2, assign_labels="discretize", 
                random_state=0).fit_predict( result[n*cadence_length: (n+1)*cadence_length, : ])
    if strong_cadence_pattern(labels):
        if screening(result[n*6: (n+1)*6, : ], labels, index)[0]:
            screen_flag, fit = screening(result[n*6: (n+1)*6, : ], labels, index)
            # Windowsize is the width of the snipet in terms of Hz
            hit_start = freq_ranges[index][0] + n*WINDOW_SIZE
            hit_end = hit_start + WINDOW_SIZE
            # Computes the frequency start and end of this given window
            return [hit_start,hit_end, fit]
    # elif screen_flag:
    #     # Windowsize is the width of the snipet in terms of Hz
    #     hit_start = freq_ranges[index][0] + n*WINDOW_SIZE
    #     hit_end = hit_start + WINDOW_SIZE
    #     # Computes the frequency start and end of this given window
    #     return [hit_start,hit_end]

# Weakest cadence pattern where anything with a on, and adjacent off pattern is accepted
def weak_cadence_pattern(labels):
    return labels[0]!=labels[1] or labels[1]!=labels[2] and labels[2]!= labels[3] or labels[3]!=labels[4] and labels[4]!=labels[5] 

# Strongest cadence pattern where only on,off,on,off,on,off patterns are accepeted. 
def strong_cadence_pattern(labels):
    return labels[0]!=labels[1] and labels[1]!=labels[2] and labels[2]!= labels[3] and labels[3]!=labels[4] and labels[4]!=labels[5] 

# Combines all the data together into one chunkc of data instead of in separate cadence samples. 
@jit(parallel=True)
def combine(data):
    new_data = np.zeros((data.shape[0]*data.shape[1],data.shape[2],data.shape[3],data.shape[4]))
    for i in prange(data.shape[0]):
        # Takes set of cadences and collapsing it down without that cadence axis, order is preserved. 
        new_data[i*data.shape[1] : (i+1)*data.shape[1],:,:,:] = data[i,:,:,:,:]
    return new_data

# computes the statistical sampling from the two layers of mean and variance
def sample_creation(inputs):
    z_mean = inputs[0]
    z_log_var = inputs[1]
    batch = tf.shape(z_mean)[0]
    dim = tf.shape(z_mean)[1]
    epsilon = tf.keras.backend.random_normal(shape=(batch, dim))
    return z_mean + tf.exp(0.5 * z_log_var) * epsilon

# Classification function
def classification_data(target_name,cadence, model, out_dir, iterations=6):
    # Create empty list to store the results
    f_hit_start = []
    f_hit_end = []
    # Get the header information
    header = Waterfall(cadence[0]).header
    # Get the maximum freq in MHz
    end = header['fch1']
    # calculate the start by taking the resolution time thes number of samples and then adding it to the maximum [it is negative resolution]
    start = header['fch1']+ header['nchans']*header['foff']
    interval = (end-start)/iterations
    # Compute the window size in MHz
    WINDOW_SIZE = abs(256*header['foff'])
    # Break down the frequency into chuncks of smaller sizes to processes
    freq_ranges = []
    for i in range(iterations):
        f_start = start+i *interval
        f_stop = start+(i+1)*(interval)
        freq_ranges.append([f_start, f_stop])
    print(freq_ranges)
    all_candidates = []
    #execution looop through each of the individual chunks of data
    for index in range(1):
        print(target_name+ " Iteration: "+str(index)+ " Range: "+str(freq_ranges[index]))
        # Get the chunk of data via the preprocessing function
        data = get_data(cadence,start =freq_ranges[index][0],end =freq_ranges[index][1])
        num_samples = data.shape[0]
        cadence_length = data.shape[1]
        # Collapse the data without the cadence axis, however keeping the order of the cadences 
        data = combine(data)
        # Feed through neural network
        net = time.time()
        result = model.predict(data, batch_size=8000, use_multiprocessing =True)[2]
        print("Push Through Neural Net: "+str(time.time()-net))
        
        # Run spectral clustering in parallel with one idle core
        cluster = time.time()

        # for name, size in sorted(((name, sys.getsizeof(value)) for name, value in locals().items()),
        #                  key= lambda x: -x[1])[:10]:
        #     print("{:>30}: {:>8}".format(name, sizeof_fmt(size)))


        with Pool(39) as p:
            candidates = p.map(functools.partial(compute_parallel, result, cadence_length,WINDOW_SIZE,index, freq_ranges), range(num_samples))
        print("Parallel Spectral Clustering: "+str(time.time()-cluster))
        # Shows the results
        final_can = [i for i in candidates if i]
        print(len(final_can))
        all_candidates.append(final_can)
    final_set = []
    for k in range(len(all_candidates)):
        for el in all_candidates[k]:
            final_set.append(el)
    print("Number of Final Candidates "+str(len(final_set)))
    df = pd.DataFrame(final_set, columns =['start_freq', 'end_freq', 'Confidence'], dtype = float)
    df.to_csv(target_name+".csv")
    



