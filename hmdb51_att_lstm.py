"""
LSTM model for action recognition
Author: Lili Meng menglili@cs.ubc.ca, March 12th, 2018
"""

from __future__ import print_function
import sys
import os
import math
import shutil
import random
import tempfile
import unittest
import traceback
import torch
import torch.utils.data
import torch.cuda
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.serialization import load_lua

import argparse

import numpy as np

use_cuda = True

class Action_Att_LSTM(nn.Module):
	def __init__(self, input_size, hidden_size, output_size, seq_len):
		super(Action_Att_LSTM, self).__init__()
		self.hidden_size = hidden_size
		self.lstm = nn.LSTM(input_size, hidden_size)
		self.fc = nn.Linear(hidden_size, output_size)
		self.fc_attention = nn.Linear(hidden_size, seq_len)
		self.dropout = nn.Dropout(p=0.5)
		self.input_size = input_size

	def forward(self, input_x):
		#output = self.batch_norm_input(input_x)

		# input_x shape [batch_size, 2048, 15] 
		# before doing LSTM, we need to swap channels as the LSTM accepts [temporal, batch_size, feature_size]
		batch_size = input_x.shape[0]
		tmp=input_x.transpose(0,2).transpose(1,2)	

		hidden = (self.init_hidden(batch_size), self.init_hidden(batch_size))

		output, hidden = self.lstm(tmp, hidden)
		#output = self.dropout(output)

		output1 = output
		#Only the last dimension of LSTM output is used, that is output[14]=output[-1] here
	

		att_weight = self.fc_attention(output1[-1])
		att_weight = F.softmax(att_weight, dim =1)

		weighted_output = torch.sum(output*att_weight.transpose(0, 1).unsqueeze(dim=2),
									dim =0)

		scores = self.fc(weighted_output)

		#using the last state of LSTM output
		#scores = self.fc(output[-1])
		# using the average state of LSTM output
		#scores = self.fc(output.mean(dim=0))
		
		print(att_weight[0:5,:])
		print("att_weight.mean(dim=0)")
		print(att_weight.mean(dim=0))
		return scores, att_weight

	def init_hidden(self, batch_size):
		result = Variable(torch.zeros(1, batch_size, self.hidden_size))
		if use_cuda:
			return result.cuda()
		else:
			return result


def lr_scheduler(optimizer, epoch_num, init_lr = 0.001, lr_decay_epochs=10):
	"""Decay learning rate by a factor of 0.1 every lr_decay_epochs.
	"""

	lr = init_lr *(0.1**(epoch_num//lr_decay_epochs))

	if epoch_num % lr_decay_epochs == 0:
		print("Learning rate changed to be : {}".format(lr))

	for param_group in optimizer.param_groups:
		param_group['lr'] = lr

	return optimizer


def train(batch_size,
		  train_data,
		  train_label,
		  model,
		  model_optimizer,
		  criterion):
	"""
	a training sample which goes through a single step of training.
	"""
	loss = 0
	model_optimizer.zero_grad()

	model_input = (train_data).view(batch_size, -1, 24)
	model_input = model_input.cuda()
	logits, att_weight = model.forward(model_input)

	loss += criterion(logits, train_label) 

	regularizer = 0
	
	att_reg = F.relu(att_weight[:, :-2] * att_weight[:, 2:] - att_weight[:, 1:-1].pow(2)).sqrt().mean()
	
	factor = FLAGS.hp_reg_factor

	if FLAGS.use_regularizer:
		loss += factor*att_reg

	loss.backward()

	model_optimizer.step()

	final_loss = loss.data[0]/batch_size

	corrects = (torch.max(logits, 1)[1].view(train_label.size()).data == train_label.data).sum()

	train_accuracy = 100.0 * corrects/batch_size

	return final_loss, train_accuracy

def test_step(batch_size,
			 batch_x,
			 batch_y,
			 model):
	
	test_data_batch = batch_x.view(batch_size, -1, 24).cuda()

	test_logits, test_model_hidden_out = model(test_data_batch)
	
	corrects = (torch.max(test_logits, 1)[1].view(batch_y.size()).data == batch_y.data).sum()

	test_accuracy = 100.0 * corrects/batch_size

	return test_logits, test_accuracy


def main():


	dataset_name = FLAGS.dataset

	maxEpoch = FLAGS.max_epoch

	num_segments = FLAGS.num_segments

	train_data = np.load("./saved_features/train_hmdb51_features.npy")
	train_label = np.load("./saved_features/train_hmdb51_labels.npy")
	train_name = np.load("./saved_features/train_hmdb51_names.npy")

	test_data = np.load("./saved_features/test_hmdb51_features.npy")
	test_label = np.load("./saved_features/test_hmdb51_labels.npy")
	test_name = np.load("./saved_features/test_hmdb51_names.npy")
	
	print("train_data.shape: ", train_data.shape)
	print("train_label.shape: ", train_label.shape)
	print("train_name.shape: ", train_name.shape)

	print("test_data.shape: ", test_data.shape)
	print("test_label.shape: ", test_label.shape)
	print("test_name.shape: ", test_name.shape)

	train_data = torch.from_numpy(train_data)
	train_label = torch.from_numpy(train_label)

	test_data = torch.from_numpy(test_data)
	test_label = torch.from_numpy(test_label)

	lstm_action = Action_Att_LSTM(input_size=2048, hidden_size=512, output_size=51, seq_len=24).cuda()
	model_optimizer = torch.optim.Adam(lstm_action.parameters(), lr=0.0001) 

	criterion = nn.CrossEntropyLoss()  

	best_test_accuracy = 0

	num_step_per_epoch_train = train_data.shape[0]//FLAGS.train_batch_size
	num_step_per_epoch_test = test_data.shape[0]//FLAGS.test_batch_size

	for epoch_num in range(maxEpoch):

		if FLAGS.use_changed_lr:
			model_optimizer = lr_scheduler(optimizer = model_optimizer, epoch_num=epoch_num, init_lr = 0.00001, lr_decay_epochs=10)
		
		lstm_action.train()
		avg_train_accuracy = 0
		permutation = torch.randperm(train_data.shape[0])
		for i in range(0, train_data.shape[0], FLAGS.train_batch_size):
			indices = permutation[i:i+FLAGS.train_batch_size]
			batch_x, batch_y, batch_name = train_data[indices], train_label[indices], train_name[indices]
			batch_x = Variable(batch_x).cuda().float()
			batch_y = Variable(batch_y).cuda().long()
			train_loss, train_accuracy = train(FLAGS.train_batch_size, batch_x, batch_y, lstm_action, model_optimizer, criterion)
			
			avg_train_accuracy+=train_accuracy
			
		final_train_accuracy = avg_train_accuracy/num_step_per_epoch_train
		print("epoch: "+str(epoch_num)+ " train accuracy: " + str(final_train_accuracy))
		

		save_train_file = FLAGS.dataset  + "_numSegments"+str(FLAGS.num_segments)+"_regFactor_"+str(FLAGS.hp_reg_factor)+"_train_acc.txt"
		with open(save_train_file, "a") as text_file:
				print(f"{str(final_train_accuracy)}", file=text_file)

		avg_test_accuracy = 0
		lstm_action.eval()
		for i in range(0, test_data.shape[0], FLAGS.test_batch_size):
			test_indices = range(test_data.shape[0])[i: i+FLAGS.test_batch_size]
			test_batch_x, test_batch_y = test_data[test_indices], test_label[test_indices]
			test_batch_x = Variable(test_batch_x).cuda().float()
			test_batch_y = Variable(test_batch_y).cuda().long()

			test_logits, test_accuracy = test_step(FLAGS.test_batch_size, test_batch_x, test_batch_y, lstm_action)
           
			avg_test_accuracy+= test_accuracy

			
		final_test_accuracy = avg_test_accuracy/num_step_per_epoch_test
		print("epoch: "+str(epoch_num)+ " test accuracy: " + str(final_test_accuracy))
		

		save_test_file = FLAGS.dataset  + "_numSegments"+str(FLAGS.num_segments)+"_regFactor_"+str(FLAGS.hp_reg_factor)+"_test_acc.txt"
		with open(save_test_file, "a") as text_file1:
				print(f"{str(final_test_accuracy)}", file=text_file1)

		if final_test_accuracy > best_test_accuracy:
			best_test_accuracy = final_test_accuracy
			print('\033[91m' + "best test accuracy is: " +str(best_test_accuracy)+ '\033[0m') 

			
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='HMDB51',
                        help='dataset: "UCF101", "HMDB51"')
    parser.add_argument('--train_batch_size', type=int, default=28,
                    	help='train_batch_size: [64]')
    parser.add_argument('--test_batch_size', type=int, default=30,
                    	help='test_batch_size: [64]')
    parser.add_argument('--max_epoch', type=int, default=100,
                    	help='max number of training epoch: [100]')
    parser.add_argument('--num_segments', type=int, default=24,
                    	help='num of segments per video: [24]')
    parser.add_argument('--use_changed_lr', dest='use_changed_lr',
    					help='not use change learning rate by default', action='store_true')
    parser.add_argument('--use_regularizer', dest='use_regularizer',
    					help='use regularizer', action='store_false')
    parser.add_argument('--hp_reg_factor', type=float, default=1,
                        help='multiply factor for regularization. [1]')
    FLAGS, unparsed = parser.parse_known_args()
    if len(unparsed) > 0:
        raise Exception('Unknown arguments:' + ', '.join(unparsed))
    print(FLAGS)
main()