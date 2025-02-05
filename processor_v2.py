import datetime
import glob
import json
import librosa
import lmdb
import math
import matplotlib.pyplot as plt
import numpy as np
import os
import pickle
import pyarrow
import python_speech_features as ps
import threading
import time
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

from os.path import join as jn
from torchlight.torchlight.io import IO

import utils.common as cmn

from net.embedding_space_evaluator import EmbeddingSpaceEvaluator
from net.ser_att_conv_rnn_v1 import AttConvRNN
from net.multimodal_context_net_v2 import PoseGeneratorTriModal as PGT, ConvDiscriminatorTriModal as CDT
from net.multimodal_context_net_v2 import PoseGenerator, AffDiscriminator
from utils import losses
from utils.average_meter import AverageMeter
from utils.data_preprocessor import DataPreprocessor
from utils.gen_utils import create_video_and_save
from utils.mocap_dataset import MoCapDataset
from utils.ted_db_utils import *


torch.manual_seed(1234)


rec_loss = losses.quat_angle_loss


def find_all_substr(a_str, sub):
    start = 0
    while True:
        start = a_str.find(sub, start)
        if start == -1:
            return
        yield start
        start += len(sub)  # use start += 1 to find overlapping matches


def get_epoch_and_loss(path_to_model_files, epoch='best'):
    all_models = os.listdir(path_to_model_files)
    if len(all_models) < 2:
        return '', None, np.inf
    if epoch == 'best':
        loss_list = -1. * np.ones(len(all_models))
        for i, model in enumerate(all_models):
            loss_val = str.split(model, '_')
            if len(loss_val) > 1:
                loss_list[i] = float(loss_val[3])
        if len(loss_list) < 3:
            best_model = all_models[np.argwhere(loss_list == min([n for n in loss_list if n > 0]))[0, 0]]
        else:
            loss_idx = np.argpartition(loss_list, 2)
            best_model = all_models[loss_idx[1]]
        all_underscores = list(find_all_substr(best_model, '_'))
        # return model name, best loss
        return best_model, int(best_model[all_underscores[0] + 1:all_underscores[1]]), \
               float(best_model[all_underscores[2] + 1:all_underscores[3]])
    assert isinstance(epoch, int)
    found_model = None
    for i, model in enumerate(all_models):
        model_epoch = str.split(model, '_')
        if len(model_epoch) > 1 and epoch == int(model_epoch[1]):
            found_model = model
            break
    if found_model is None:
        return '', None, np.inf
    all_underscores = list(find_all_substr(found_model, '_'))
    return found_model, int(found_model[all_underscores[0] + 1:all_underscores[1]]), \
           float(found_model[all_underscores[2] + 1:all_underscores[3]])


class Processor(object):
    """
        Processor for emotive gesture generation
    """

    def __init__(self, base_path, args, s2ag_config_args, data_loader, pose_dim, coords,
                 audio_sr, min_train_epochs=20, zfill=6):
        self.device = torch.device('cuda:{}'.format(torch.cuda.current_device())
                                   if torch.cuda.is_available() else 'cpu')
        self.base_path = base_path
        self.args = args
        self.s2ag_config_args = s2ag_config_args
        self.data_loader = data_loader
        self.result = dict()
        self.iter_info = dict()
        self.epoch_info = dict()
        self.meta_info = dict(epoch=0, iter=0)
        self.io = IO(
            self.args.work_dir_s2ag,
            save_log=self.args.save_log,
            print_log=self.args.print_log)

        # model
        self.pose_dim = pose_dim
        self.coords = coords
        self.audio_sr = audio_sr

        if self.args.train_s2ag:
            self.time_steps = self.data_loader['train_data_s2ag'].n_poses
            self.audio_length = self.data_loader['train_data_s2ag'].expected_audio_length
            self.spectrogram_length = self.data_loader['train_data_s2ag'].expected_spectrogram_length
            self.num_mfcc = self.data_loader['train_data_s2ag'].num_mfcc_combined
            self.lang_model = self.data_loader['train_data_s2ag'].lang_model
        else:
            self.time_steps = self.data_loader['test_data_s2ag'].n_poses
            self.audio_length = self.data_loader['test_data_s2ag'].expected_audio_length
            self.spectrogram_length = self.data_loader['test_data_s2ag'].expected_spectrogram_length
            self.num_mfcc = self.data_loader['test_data_s2ag'].num_mfcc_combined
            self.lang_model = self.data_loader['test_data_s2ag'].lang_model
        self.mfcc_length = int(np.ceil(self.audio_length / 512))

        self.best_s2ag_loss = np.inf
        self.best_s2ag_loss_epoch = None
        self.s2ag_loss_updated = False
        self.min_train_epochs = min_train_epochs
        self.zfill = zfill

        self.train_speaker_model = self.data_loader['train_data_s2ag'].speaker_model
        self.val_speaker_model = self.data_loader['val_data_s2ag'].speaker_model
        self.test_speaker_model = self.data_loader['test_data_s2ag'].speaker_model
        self.trimodal_generator = PGT(self.s2ag_config_args,
                                      pose_dim=self.pose_dim,
                                      n_words=self.lang_model.n_words,
                                      word_embed_size=self.s2ag_config_args.wordembed_dim,
                                      word_embeddings=self.lang_model.word_embedding_weights,
                                      z_obj=self.train_speaker_model)
        self.trimodal_discriminator = CDT(self.pose_dim)
        self.use_mfcc = True
        if self.use_mfcc:
            self.s2ag_generator = PoseGenerator(self.s2ag_config_args,
                                                pose_dim=self.pose_dim,
                                                n_words=self.lang_model.n_words,
                                                word_embed_size=self.s2ag_config_args.wordembed_dim,
                                                word_embeddings=self.lang_model.word_embedding_weights,
                                                mfcc_length=self.mfcc_length,
                                                num_mfcc=self.num_mfcc,
                                                time_steps=self.time_steps,
                                                z_obj=self.train_speaker_model)
        else:
            self.s2ag_generator = PGT(self.s2ag_config_args,
                                      pose_dim=self.pose_dim,
                                      n_words=self.lang_model.n_words,
                                      word_embed_size=self.s2ag_config_args.wordembed_dim,
                                      word_embeddings=self.lang_model.word_embedding_weights,
                                      z_obj=self.train_speaker_model)
        # self.s2ag_discriminator = CDT(self.pose_dim)
        self.s2ag_discriminator = AffDiscriminator(self.pose_dim)
        self.evaluator_trimodal = EmbeddingSpaceEvaluator(self.base_path, self.s2ag_config_args, self.pose_dim,
                                                          self.lang_model, self.device)
        self.evaluator = EmbeddingSpaceEvaluator(self.base_path, self.s2ag_config_args, self.pose_dim,
                                                 self.lang_model, self.device)

        if self.args.use_multiple_gpus and torch.cuda.device_count() > 1:
            self.args.batch_size *= torch.cuda.device_count()
            self.trimodal_generator = nn.DataParallel(self.trimodal_generator)
            self.trimodal_discriminator = nn.DataParallel(self.trimodal_discriminator)
            self.s2ag_generator = nn.DataParallel(self.s2ag_generator)
            self.s2ag_discriminator = nn.DataParallel(self.s2ag_discriminator)
        else:
            self.trimodal_generator.to(self.device)
            self.trimodal_discriminator.to(self.device)
            self.s2ag_generator.to(self.device)
            self.s2ag_discriminator.to(self.device)

        npz_path = jn(self.args.data_path, self.args.dataset_s2ag, 'npz')
        os.makedirs(npz_path, exist_ok=True)
        self.num_test_samples = self.data_loader['test_data_s2ag'].n_samples

        if self.args.train_s2ag:
            self.num_train_samples = self.data_loader['train_data_s2ag'].n_samples
            self.num_val_samples = self.data_loader['val_data_s2ag'].n_samples
            self.num_total_samples = self.num_train_samples + self.num_val_samples + self.num_test_samples
            print('Total s2ag training data:\t\t{:>6} ({:.2f}%)'.format(
                self.num_train_samples, 100. * self.num_train_samples / self.num_total_samples))
            print('Training s2ag with batch size:\t{:>6}'.format(self.args.batch_size))
            train_dir_name = jn(npz_path, 'train')
            if not os.path.exists(train_dir_name):
                self.save_cache('train', train_dir_name)
            self.load_cache('train', train_dir_name)
            print('Total s2ag validation data:\t\t{:>6} ({:.2f}%)'.format(
                self.num_val_samples, 100. * self.num_val_samples / self.num_total_samples))
            val_dir_name = jn(npz_path, 'val')
            if not os.path.exists(val_dir_name):
                self.save_cache('val', val_dir_name)
            self.load_cache('val', val_dir_name)
        else:
            self.train_samples = None
            self.val_samples = None
            self.num_total_samples = self.num_test_samples

        print('Total s2ag testing data:\t\t{:>6} ({:.2f}%)'.format(
            self.num_test_samples, 100. * self.num_test_samples / self.num_total_samples))
        test_dir_name = jn(npz_path, 'test')
        if not os.path.exists(test_dir_name):
            self.save_cache('test', test_dir_name)

        self.lr_s2ag_gen = self.s2ag_config_args.learning_rate
        self.lr_s2ag_dis = self.s2ag_config_args.learning_rate * self.s2ag_config_args.discriminator_lr_weight

        # s2ag optimizers
        self.s2ag_gen_optimizer = optim.Adam(self.s2ag_generator.parameters(),
                                             lr=self.lr_s2ag_gen, betas=(0.5, 0.999))
        self.s2ag_dis_optimizer = torch.optim.Adam(
            self.s2ag_discriminator.parameters(),
            lr=self.lr_s2ag_dis,
            betas=(0.5, 0.999))

    def load_cache(self, part, dir_name, load_full=True):
        print('Loading {} cache'.format(part), end='')
        if load_full:
            start_time = time.time()
            npz = np.load(jn(dir_name, '../full', part + '.npz'), allow_pickle=True)
            samples_dict = {'extended_word_seq': npz['extended_word_seq'],
                            'vec_seq': npz['vec_seq'],
                            'audio': npz['audio'],
                            'audio_max': npz['audio_max'],
                            'mfcc_features': npz['mfcc_features'].astype(np.float16),
                            'vid_indices': npz['vid_indices']
                            }
            if part == 'train':
                self.train_samples = samples_dict
            elif part == 'val':
                self.val_samples = samples_dict
            elif part == 'test':
                self.test_samples = samples_dict
            print(' took {:>6} seconds.'.format(int(np.ceil(time.time() - start_time))))
        else:
            num_samples = self.num_train_samples if part == 'train' else (self.num_val_samples if part == 'val' else self.num_test_samples)
            samples_dict = {'extended_word_seq': [],
                            'vec_seq': [],
                            'audio': [],
                            'audio_max': [],
                            'mfcc_features': [],
                            'vid_indices': []}
            for k in range(num_samples):
                start_time = time.time()
                npz = np.load(jn(dir_name, str(k).zfill(6) + '.npz'), allow_pickle=True)
                samples_dict['extended_word_seq'].append(npz['extended_word_seq'])
                samples_dict['vec_seq'].append(npz['vec_seq'])
                samples_dict['audio'].append(npz['audio'])
                samples_dict['audio_max'].append(npz['audio_max'])
                samples_dict['mfcc_features'].append(npz['mfcc_features'].astype(np.float16))
                samples_dict['vid_indices'].append(npz['vid_indices'])
                time_taken = time.time() - start_time
                time_remaining = np.ceil((num_samples - k - 1) * time_taken)
                print('\rLoading {} cache {:>6}/{}, estimated time remaining {}.'.format(part, k + 1, num_samples,
                                                                                        str(datetime.timedelta(seconds=time_remaining))), end='')
            for dict_key in samples_dict.keys():
                samples_dict[dict_key] = np.stack(samples_dict[dict_key])
        
        if part == 'train':
            self.train_samples = samples_dict
        elif part == 'val':
            self.val_samples = samples_dict
        elif part == 'test':
            self.test_samples = samples_dict
        print(' Completed.')

    def save_cache(self, part, dir_name):
        data_s2ag = self.data_loader['{}_data_s2ag'.format(part)]
        num_samples = self.num_train_samples if part == 'train' else (self.num_val_samples if part == 'val' else self.num_test_samples)
        speaker_model = self.train_speaker_model if part == 'train' else (self.val_speaker_model if part == 'val' else self.test_speaker_model)

        extended_word_seq_all = np.zeros((num_samples, self.time_steps), dtype=np.int64)
        vec_seq_all = np.zeros((num_samples, self.time_steps, self.pose_dim))
        audio_all = np.zeros((num_samples, self.audio_length), dtype=np.int16)
        audio_max_all = np.zeros(num_samples)
        mfcc_features_all = np.zeros((num_samples, self.num_mfcc, self.mfcc_length))
        vid_indices_all = np.zeros(num_samples, dtype=np.int64)
        print('Caching {} data {:>6}/{}.'.format(part, 0, num_samples), end='')
        for k in range(num_samples):
            with data_s2ag.lmdb_env.begin(write=False) as txn:
                key = '{:010}'.format(k).encode('ascii')
                sample = txn.get(key)
                sample = pyarrow.deserialize(sample)
                word_seq, pose_seq, vec_seq, audio, spectrogram, mfcc_features, aux_info = sample
            # with data_s2ag.lmdb_env.begin(write=False) as txn:
            #     key = '{:010}'.format(k).encode('ascii')
            #     sample = txn.get(key)
            #     sample = pyarrow.deserialize(sample)
            #     word_seq, pose_seq, vec_seq, audio, spectrogram, mfcc_features, aux_info = sample

            duration = aux_info['end_time'] - aux_info['start_time']
            audio_max_all[k] = np.max(np.abs(audio))
            do_clipping = True

            if do_clipping:
                sample_end_time = aux_info['start_time'] + duration * data_s2ag.n_poses / vec_seq.shape[0]
                audio = make_audio_fixed_length(audio, self.audio_length)
                mfcc_features = mfcc_features[:, 0:self.mfcc_length]
                vec_seq = vec_seq[0:data_s2ag.n_poses]
            else:
                sample_end_time = None

            # to tensors
            word_seq_tensor = Processor.words_to_tensor(data_s2ag.lang_model, word_seq, sample_end_time)
            extended_word_seq = Processor.extend_word_seq(data_s2ag.n_poses, data_s2ag.lang_model,
                                                          data_s2ag.remove_word_timing, word_seq,
                                                          aux_info, sample_end_time).detach().cpu().numpy()
            vec_seq = torch.from_numpy(vec_seq).reshape((vec_seq.shape[0], -1)).float().detach().cpu().numpy()

            extended_word_seq_all[k] = extended_word_seq
            vec_seq_all[k] = vec_seq
            audio_all[k] = np.int16(audio / audio_max_all[k] * 32767)
            mfcc_features_all[k] = mfcc_features
            vid_indices_all[k] = speaker_model.word2index[aux_info['vid']]
            
            np.savez_compressed(jn(dir_name, part, str(k).zfill(6) + '.npz'),
                                extended_word_seq=extended_word_seq,
                                vec_seq=vec_seq,
                                audio=np.int16(audio / audio_max_all[k] * 32767),
                                audio_max=audio_max_all[k],
                                mfcc_features=mfcc_features,
                                vid_indices=vid_indices_all[k])
            print('\rCaching {} data {:>6}/{}.'.format(part, k + 1, num_samples), end='')

        print('\t Storing full cache', end='')
        full_cache_path = jn(dir_name, '../full')
        os.makedirs(full_cache_path, exist_ok=True)
        np.savez_compressed(jn(full_cache_path, part + '.npz'),
                            extended_word_seq=extended_word_seq_all,
                            vec_seq=vec_seq_all, audio=audio_all, audio_max=audio_max_all,
                            mfcc_features=mfcc_features_all,
                            vid_indices=vid_indices_all)
        print(' done.')

    def process_data(self, data, poses, quat, trans, affs):
        data = data.float().to(self.device)
        poses = poses.float().to(self.device)
        quat = quat.float().to(self.device)
        trans = trans.float().to(self.device)
        affs = affs.float().to(self.device)
        return data, poses, quat, trans, affs

    def load_model_at_epoch(self, epoch='best'):
        model_name, self.best_s2ag_loss_epoch, self.best_s2ag_loss = \
            get_epoch_and_loss(self.args.work_dir_s2ag, epoch=epoch)
        model_found = False
        try:
            loaded_vars = torch.load(jn(self.args.work_dir_s2ag, model_name))
            self.s2ag_generator.load_state_dict(loaded_vars['gen_model_dict'])
            self.s2ag_discriminator.load_state_dict(loaded_vars['dis_model_dict'])
            model_found = True
        except (FileNotFoundError, IsADirectoryError):
            if epoch == 'best':
                print('Warning! No saved model found.')
            else:
                print('Warning! No saved model found at epoch {}.'.format(epoch))
        return model_found

    def adjust_lr_s2ag(self):
        self.lr_s2ag_gen = self.lr_s2ag_gen * self.args.lr_s2ag_decay
        for param_group in self.s2ag_gen_optimizer.param_groups:
            param_group['lr'] = self.lr_s2ag_gen

        self.lr_s2ag_dis = self.lr_s2ag_dis * self.args.lr_s2ag_decay
        for param_group in self.s2ag_dis_optimizer.param_groups:
            param_group['lr'] = self.lr_s2ag_dis

    def show_epoch_info(self):

        best_metrics = [self.best_s2ag_loss]
        print_epochs = [self.best_s2ag_loss_epoch
                        if self.best_s2ag_loss_epoch is not None else 0] * len(best_metrics)
        i = 0
        for k, v in self.epoch_info.items():
            self.io.print_log('\t{}: {}. Best so far: {:.4f} (epoch: {:d}).'.
                              format(k, v, best_metrics[i], print_epochs[i]))
            i += 1
        if self.args.pavi_log:
            self.io.log('train', self.meta_info['iter'], self.epoch_info)

    def show_iter_info(self):

        if self.meta_info['iter'] % self.args.log_interval == 0:
            info = '\tIter {} Done.'.format(self.meta_info['iter'])
            for k, v in self.iter_info.items():
                if isinstance(v, float):
                    info = info + ' | {}: {:.4f}'.format(k, v)
                else:
                    info = info + ' | {}: {}'.format(k, v)

            self.io.print_log(info)

            if self.args.pavi_log:
                self.io.log('train', self.meta_info['iter'], self.iter_info)

    def count_parameters(self):
        return sum(p.numel() for p in self.s2ag_generator.parameters() if p.requires_grad)

    @staticmethod
    def extend_word_seq(n_frames, lang, remove_word_timing, words, aux_info, end_time=None):
        if end_time is None:
            end_time = aux_info['end_time']
        frame_duration = (end_time - aux_info['start_time']) / n_frames

        extended_word_indices = np.zeros(n_frames)  # zero is the index of padding token
        if remove_word_timing:
            n_words = 0
            for word in words:
                idx = max(0, int(np.floor((word[1] - aux_info['start_time']) / frame_duration)))
                if idx < n_frames:
                    n_words += 1
            space = int(n_frames / (n_words + 1))
            for word_idx in range(n_words):
                idx = (word_idx + 1) * space
                extended_word_indices[idx] = lang.get_word_index(words[word_idx][0])
        else:
            prev_idx = 0
            for word in words:
                idx = max(0, int(np.floor((word[1] - aux_info['start_time']) / frame_duration)))
                if idx < n_frames:
                    extended_word_indices[idx] = lang.get_word_index(word[0])
                    # extended_word_indices[prev_idx:idx+1] = lang.get_word_index(word[0])
                    prev_idx = idx
        return torch.Tensor(extended_word_indices).long()

    @staticmethod
    def words_to_tensor(lang, words, end_time=None):
        indexes = [lang.SOS_token]
        for word in words:
            if end_time is not None and word[1] > end_time:
                break
            indexes.append(lang.get_word_index(word[0]))
        indexes.append(lang.EOS_token)
        return torch.Tensor(indexes).long()

    def yield_batch_old(self, train):
        batch_word_seq_tensor = torch.zeros((self.args.batch_size, self.time_steps)).long().to(self.device)
        batch_word_seq_lengths = torch.zeros(self.args.batch_size).long().to(self.device)
        batch_extended_word_seq = torch.zeros((self.args.batch_size, self.time_steps)).long().to(self.device)
        batch_pose_seq = torch.zeros((self.args.batch_size, self.time_steps,
                                      self.pose_dim + self.coords)).float().to(self.device)
        batch_vec_seq = torch.zeros((self.args.batch_size, self.time_steps, self.pose_dim)).float().to(self.device)
        batch_audio = torch.zeros((self.args.batch_size, self.audio_length)).float().to(self.device)
        batch_spectrogram = torch.zeros((self.args.batch_size, 128,
                                         self.spectrogram_length)).float().to(self.device)
        batch_mfcc = torch.zeros((self.args.batch_size, self.num_mfcc,
                                  self.mfcc_length)).float().to(self.device)
        batch_vid_indices = torch.zeros(self.args.batch_size).long().to(self.device)

        if train:
            data_s2ag = self.data_loader['train_data_s2ag']
            num_data = self.num_train_samples
        else:
            data_s2ag = self.data_loader['val_data_s2ag']
            num_data = self.num_val_samples

        pseudo_passes = (num_data + self.args.batch_size - 1) // self.args.batch_size
        prob_dist = np.ones(num_data) / float(num_data)

        # def load_from_txn(_txn, _i, _k):
        #     key = '{:010}'.format(_k).encode('ascii')
        #     sample = _txn.get(key)
        #     sample = pyarrow.deserialize(sample)
        #     word_seq, pose_seq, vec_seq, audio, spectrogram, mfcc_features, aux_info = sample
        #
        #     # vid_name = sample[-1]['vid']
        #     # clip_start = str(sample[-1]['start_time'])
        #     # clip_end = str(sample[-1]['end_time'])
        #
        #     duration = aux_info['end_time'] - aux_info['start_time']
        #     do_clipping = True
        #
        #     if do_clipping:
        #         sample_end_time = aux_info['start_time'] + duration * data_s2ag.n_poses / vec_seq.shape[0]
        #         audio = make_audio_fixed_length(audio, self.audio_length)
        #         spectrogram = spectrogram[:, 0:self.spectrogram_length]
        #         mfcc_features = mfcc_features[:, 0:self.mfcc_length]
        #         vec_seq = vec_seq[0:data_s2ag.n_poses]
        #         pose_seq = pose_seq[0:data_s2ag.n_poses]
        #     else:
        #         sample_end_time = None
        #
        #     # to tensors
        #     word_seq_tensor = Processor.words_to_tensor(data_s2ag.lang_model, word_seq, sample_end_time)
        #     extended_word_seq = Processor.extend_word_seq(data_s2ag.n_poses, data_s2ag.lang_model,
        #                                                   data_s2ag.remove_word_timing, word_seq,
        #                                                   aux_info, sample_end_time)
        #     vec_seq = torch.from_numpy(vec_seq).reshape((vec_seq.shape[0], -1)).float()
        #     pose_seq = torch.from_numpy(pose_seq).reshape((pose_seq.shape[0], -1)).float()
        #     # scaled_audio = np.int16(audio / np.max(np.abs(audio)) * self.audio_length)
        #     mfcc_features = torch.from_numpy(mfcc_features).float()
        #     audio = torch.from_numpy(audio).float()
        #     spectrogram = torch.from_numpy(spectrogram)
        #
        #     batch_word_seq_tensor[_i, :len(word_seq_tensor)] = word_seq_tensor
        #     batch_word_seq_lengths[_i] = len(word_seq_tensor)
        #     batch_extended_word_seq[_i] = extended_word_seq
        #     batch_pose_seq[_i] = pose_seq
        #     batch_vec_seq[_i] = vec_seq
        #     batch_audio[_i] = audio
        #     batch_spectrogram[_i] = spectrogram
        #     batch_mfcc[_i] = mfcc_features
        #     # speaker input
        #     if train:
        #         if self.train_speaker_model and self.train_speaker_model.__class__.__name__ == 'Vocab':
        #             batch_vid_indices[_i] = \
        #                 torch.LongTensor([self.train_speaker_model.word2index[aux_info['vid']]])
        #     else:
        #         if self.val_speaker_model and self.val_speaker_model.__class__.__name__ == 'Vocab':
        #             batch_vid_indices[_i] = \
        #                 torch.LongTensor([self.val_speaker_model.word2index[aux_info['vid']]])

        for p in range(pseudo_passes):
            rand_keys = np.random.choice(num_data, size=self.args.batch_size, replace=True, p=prob_dist)
            for i, k in enumerate(rand_keys):
                if train:
                    word_seq = self.train_samples['word_seq'].item()[str(k).zfill(6)]
                    pose_seq = self.train_samples['pose_seq'][k]
                    vec_seq = self.train_samples['vec_seq'][k]
                    audio = self.train_samples['audio'][k] / 32767 * self.train_samples['audio_max'][k]
                    mfcc_features = self.train_samples['mfcc_features'][k]
                    aux_info = self.train_samples['aux_info'].item()[str(k).zfill(6)]
                else:
                    word_seq = self.val_samples['word_seq'].item()[str(k).zfill(6)]
                    pose_seq = self.val_samples['pose_seq'][k]
                    vec_seq = self.val_samples['vec_seq'][k]
                    audio = self.val_samples['audio'][k] / 32767 * self.val_samples['audio_max'][k]
                    mfcc_features = self.val_samples['mfcc_features'][k]
                    aux_info = self.val_samples['aux_info'].item()[str(k).zfill(6)]

                duration = aux_info['end_time'] - aux_info['start_time']
                do_clipping = True

                if do_clipping:
                    sample_end_time = aux_info['start_time'] + duration * data_s2ag.n_poses / vec_seq.shape[0]
                    audio = make_audio_fixed_length(audio, self.audio_length)
                    mfcc_features = mfcc_features[:, 0:self.mfcc_length]
                    vec_seq = vec_seq[0:data_s2ag.n_poses]
                    pose_seq = pose_seq[0:data_s2ag.n_poses]
                else:
                    sample_end_time = None

                # to tensors
                word_seq_tensor = Processor.words_to_tensor(data_s2ag.lang_model, word_seq, sample_end_time)
                extended_word_seq = Processor.extend_word_seq(data_s2ag.n_poses, data_s2ag.lang_model,
                                                              data_s2ag.remove_word_timing, word_seq,
                                                              aux_info, sample_end_time)
                vec_seq = torch.from_numpy(vec_seq).reshape((vec_seq.shape[0], -1)).float()
                pose_seq = torch.from_numpy(pose_seq).reshape((pose_seq.shape[0], -1)).float()
                # scaled_audio = np.int16(audio / np.max(np.abs(audio)) * self.audio_length)
                mfcc_features = torch.from_numpy(mfcc_features).float()
                audio = torch.from_numpy(audio).float()

                batch_word_seq_tensor[i, :len(word_seq_tensor)] = word_seq_tensor
                batch_word_seq_lengths[i] = len(word_seq_tensor)
                batch_extended_word_seq[i] = extended_word_seq
                batch_pose_seq[i] = pose_seq
                batch_vec_seq[i] = vec_seq
                batch_audio[i] = audio
                batch_mfcc[i] = mfcc_features
                # speaker input
                if train:
                    if self.train_speaker_model and self.train_speaker_model.__class__.__name__ == 'Vocab':
                        batch_vid_indices[i] = \
                            torch.LongTensor([self.train_speaker_model.word2index[aux_info['vid']]])
                else:
                    if self.val_speaker_model and self.val_speaker_model.__class__.__name__ == 'Vocab':
                        batch_vid_indices[i] = \
                            torch.LongTensor([self.val_speaker_model.word2index[aux_info['vid']]])

            # with data_s2ag.lmdb_env.begin(write=False) as txn:
            #     threads = []
            #     for i, k in enumerate(rand_keys):
            #         threads.append(threading.Thread(target=load_from_txn, args=[i, k]))
            #         threads[i].start()
            #     for i in range(len(rand_keys)):
            #         threads[i].join()
            yield batch_word_seq_tensor, batch_word_seq_lengths, batch_extended_word_seq, batch_pose_seq, \
                batch_vec_seq, batch_audio, batch_spectrogram, batch_mfcc, batch_vid_indices

    def yield_batch(self, train):
        if train:
            data_s2ag = self.data_loader['train_data_s2ag']
            num_data = self.num_train_samples
        else:
            data_s2ag = self.data_loader['val_data_s2ag']
            num_data = self.num_val_samples

        pseudo_passes = (num_data + self.args.batch_size - 1) // self.args.batch_size
        prob_dist = np.ones(num_data) / float(num_data)

        for p in range(pseudo_passes):
            rand_keys = np.random.choice(num_data, size=self.args.batch_size, replace=True, p=prob_dist)
            if train:
                batch_extended_word_seq = torch.from_numpy(
                    self.train_samples['extended_word_seq'][rand_keys]).to(self.device)
                batch_vec_seq = torch.from_numpy(self.train_samples['vec_seq'][rand_keys]).float().to(self.device)
                batch_audio = torch.from_numpy(
                    self.train_samples['audio'][rand_keys] *
                    self.train_samples['audio_max'][rand_keys, None] / 32767).float().to(self.device)
                batch_mfcc_features = torch.from_numpy(
                    self.train_samples['mfcc_features'][rand_keys]).float().to(self.device)
                curr_vid_indices = self.train_samples['vid_indices'][rand_keys]
            else:
                batch_extended_word_seq = torch.from_numpy(
                    self.val_samples['extended_word_seq'][rand_keys]).to(self.device)
                batch_vec_seq = torch.from_numpy(self.val_samples['vec_seq'][rand_keys]).float().to(self.device)
                batch_audio = torch.from_numpy(
                    self.val_samples['audio'][rand_keys] *
                    self.val_samples['audio_max'][rand_keys, None] / 32767).float().to(self.device)
                batch_mfcc_features = torch.from_numpy(
                    self.val_samples['mfcc_features'][rand_keys]).float().to(self.device)
                curr_vid_indices = self.val_samples['vid_indices'][rand_keys]

            # speaker input
            batch_vid_indices = None
            if train and self.train_speaker_model and\
                    self.train_speaker_model.__class__.__name__ == 'Vocab':
                batch_vid_indices = torch.LongTensor([
                    np.random.choice(np.setdiff1d(list(self.train_speaker_model.word2index.values()),
                                                  curr_vid_indices))
                    for _ in range(self.args.batch_size)]).to(self.device)
            elif self.val_speaker_model and\
                    self.val_speaker_model.__class__.__name__ == 'Vocab':
                batch_vid_indices = torch.LongTensor([
                    np.random.choice(np.setdiff1d(list(self.val_speaker_model.word2index.values()),
                                                  curr_vid_indices))
                    for _ in range(self.args.batch_size)]).to(self.device)

            yield batch_extended_word_seq, batch_vec_seq, batch_audio, batch_mfcc_features, batch_vid_indices

    def return_batch(self, batch_size, randomized=True):

        data_s2ag = self.data_loader['test_data_s2ag']

        if len(batch_size) > 1:
            rand_keys = np.copy(batch_size)
            batch_size = len(batch_size)
        else:
            batch_size = batch_size[0]
            prob_dist = np.ones(self.num_test_samples) / float(self.num_test_samples)
            if randomized:
                rand_keys = np.random.choice(self.num_test_samples, size=batch_size, replace=False, p=prob_dist)
            else:
                rand_keys = np.arange(batch_size)

        batch_words = [[] for _ in range(batch_size)]
        batch_aux_info = [[] for _ in range(batch_size)]
        batch_word_seq_tensor = torch.zeros((batch_size, self.time_steps)).long().to(self.device)
        batch_word_seq_lengths = torch.zeros(batch_size).long().to(self.device)
        batch_extended_word_seq = torch.zeros((batch_size, self.time_steps)).long().to(self.device)
        batch_pose_seq = torch.zeros((batch_size, self.time_steps,
                                      self.pose_dim + self.coords)).float().to(self.device)
        batch_vec_seq = torch.zeros((batch_size, self.time_steps, self.pose_dim)).float().to(self.device)
        batch_target_seq = torch.zeros((batch_size, self.time_steps, self.pose_dim)).float().to(self.device)
        batch_audio = torch.zeros((batch_size, self.audio_length)).float().to(self.device)
        batch_spectrogram = torch.zeros((batch_size, 128,
                                         self.spectrogram_length)).float().to(self.device)
        batch_mfcc = torch.zeros((batch_size, self.num_mfcc,
                                  self.mfcc_length)).float().to(self.device)

        for i, k in enumerate(rand_keys):

            with data_s2ag.lmdb_env.begin(write=False) as txn:
                key = '{:010}'.format(k).encode('ascii')
                sample = txn.get(key)
                sample = pyarrow.deserialize(sample)
                word_seq, pose_seq, vec_seq, audio, spectrogram, mfcc_features, aux_info = sample

                # for selected_vi in range(len(word_seq)):  # make start time of input text zero
                #     word_seq[selected_vi][1] -= aux_info['start_time']  # start time
                #     word_seq[selected_vi][2] -= aux_info['start_time']  # end time
                batch_words[i] = [word_seq[i][0] for i in range(len(word_seq))]
                batch_aux_info[i] = aux_info

                duration = aux_info['end_time'] - aux_info['start_time']
                do_clipping = True

                if do_clipping:
                    sample_end_time = aux_info['start_time'] + duration * data_s2ag.n_poses / vec_seq.shape[0]
                    audio = make_audio_fixed_length(audio, self.audio_length)
                    spectrogram = spectrogram[:, 0:self.spectrogram_length]
                    mfcc_features = mfcc_features[:, 0:self.mfcc_length]
                    vec_seq = vec_seq[0:data_s2ag.n_poses]
                    pose_seq = pose_seq[0:data_s2ag.n_poses]
                else:
                    sample_end_time = None

                # to tensors
                word_seq_tensor = Processor.words_to_tensor(data_s2ag.lang_model, word_seq, sample_end_time)
                extended_word_seq = Processor.extend_word_seq(data_s2ag.n_poses, data_s2ag.lang_model,
                                                              data_s2ag.remove_word_timing, word_seq,
                                                              aux_info, sample_end_time)
                vec_seq = torch.from_numpy(vec_seq).reshape((vec_seq.shape[0], -1)).float()
                pose_seq = torch.from_numpy(pose_seq).reshape((pose_seq.shape[0], -1)).float()
                target_seq = convert_pose_seq_to_dir_vec(pose_seq)
                target_seq = target_seq.reshape(target_seq.shape[0], -1)
                target_seq -= np.reshape(self.s2ag_config_args.mean_dir_vec, -1)
                mfcc_features = torch.from_numpy(mfcc_features)
                audio = torch.from_numpy(audio).float()
                spectrogram = torch.from_numpy(spectrogram)

                batch_word_seq_tensor[i, :len(word_seq_tensor)] = word_seq_tensor
                batch_word_seq_lengths[i] = len(word_seq_tensor)
                batch_extended_word_seq[i] = extended_word_seq
                batch_pose_seq[i] = pose_seq
                batch_vec_seq[i] = vec_seq
                batch_target_seq[i] = torch.from_numpy(target_seq).float()
                batch_audio[i] = audio
                batch_spectrogram[i] = spectrogram
                batch_mfcc[i] = mfcc_features
                # speaker input
                # if self.test_speaker_model and self.test_speaker_model.__class__.__name__ == 'Vocab':
                    # batch_vid_indices[i] = \
                    #     torch.LongTensor([self.test_speaker_model.word2index[aux_info['vid']]])
        batch_vid_indices = torch.LongTensor(
            [np.random.choice(list(self.test_speaker_model.word2index.values()))
             for _ in range(batch_size)]).to(self.device)

        return batch_words, batch_aux_info, batch_word_seq_tensor, batch_word_seq_lengths, \
            batch_extended_word_seq, batch_pose_seq, batch_vec_seq, batch_target_seq, batch_audio, \
            batch_spectrogram, batch_mfcc, batch_vid_indices

    @staticmethod
    def add_noise(data):
        noise = torch.randn_like(data) * 0.1
        return data + noise

    @staticmethod
    def push_samples(evaluator, target, out_dir_vec, in_text_padded, in_audio,
                     losses_all, joint_mae, accel, mean_dir_vec, n_poses, n_pre_poses):

        batch_size = len(target)

        # if evaluator:
        #     evaluator.reset()

        loss = F.l1_loss(out_dir_vec, target)

        losses_all.update(loss.item(), batch_size)

        if evaluator:
            evaluator.push_samples(in_text_padded, in_audio, out_dir_vec, target)

        # calculate MAE of joint coordinates
        out_dir_vec_np = out_dir_vec.detach().cpu().numpy()
        out_dir_vec_np += np.array(mean_dir_vec).squeeze()
        out_joint_poses = convert_dir_vec_to_pose(out_dir_vec_np)
        target_vec = target.detach().cpu().numpy()
        target_vec += np.array(mean_dir_vec).squeeze()
        target_poses = convert_dir_vec_to_pose(target_vec)

        if out_joint_poses.shape[1] == n_poses:
            diff = out_joint_poses[:, n_pre_poses:] - \
                   target_poses[:, n_pre_poses:]
        else:
            diff = out_joint_poses - target_poses[:, n_pre_poses:]
        mae_val = np.mean(np.absolute(diff))
        joint_mae.update(mae_val, batch_size)

        # accel
        target_acc = np.diff(target_poses, n=2, axis=1)
        out_acc = np.diff(out_joint_poses, n=2, axis=1)
        accel.update(np.mean(np.abs(target_acc - out_acc)), batch_size)

        return evaluator, losses_all, joint_mae, accel

    def forward_pass_s2ag(self, in_text, in_audio, in_mfcc, target_poses, vid_indices, train,
                          target_seq=None, words=None, aux_info=None, save_path=None, make_video=False,
                          calculate_metrics=False, losses_all_trimodal=None, joint_mae_trimodal=None,
                          accel_trimodal=None, losses_all=None, joint_mae=None, accel=None):
        warm_up_epochs = self.s2ag_config_args.loss_warmup
        use_noisy_target = False

        # make pre seq input
        pre_seq = target_poses.new_zeros((target_poses.shape[0], target_poses.shape[1],
                                          target_poses.shape[2] + 1))
        pre_seq[:, 0:self.s2ag_config_args.n_pre_poses, :-1] =\
            target_poses[:, 0:self.s2ag_config_args.n_pre_poses]
        pre_seq[:, 0:self.s2ag_config_args.n_pre_poses, -1] = 1  # indicating bit for constraints

        ###########################################################################################
        # train D
        dis_error = None
        if self.meta_info['epoch'] > warm_up_epochs and self.s2ag_config_args.loss_gan_weight > 0.0:
            self.s2ag_dis_optimizer.zero_grad()

            # out shape (batch x seq x dim)
            if self.use_mfcc:
                out_dir_vec, *_ = self.s2ag_generator(pre_seq, in_text, in_mfcc, vid_indices)
            else:
                out_dir_vec, *_ = self.s2ag_generator(pre_seq, in_text, in_audio, vid_indices)

            if use_noisy_target:
                noise_target = Processor.add_noise(target_poses)
                noise_out = Processor.add_noise(out_dir_vec.detach())
                dis_real = self.s2ag_discriminator(noise_target, in_text)
                dis_fake = self.s2ag_discriminator(noise_out, in_text)
            else:
                dis_real = self.s2ag_discriminator(target_poses, in_text)
                dis_fake = self.s2ag_discriminator(out_dir_vec.detach(), in_text)

            dis_error = torch.sum(-torch.mean(torch.log(dis_real + 1e-8) + torch.log(1 - dis_fake + 1e-8)))  # ns-gan
            if train:
                dis_error.backward()
                self.s2ag_dis_optimizer.step()

        ###########################################################################################
        # train G
        self.s2ag_gen_optimizer.zero_grad()

        # decoding
        out_dir_vec_trimodal, *_ = self.trimodal_generator(pre_seq, in_text, in_audio, vid_indices)
        if self.use_mfcc:
            out_dir_vec, z, z_mu, z_log_var = self.s2ag_generator(pre_seq, in_text, in_mfcc, vid_indices)
        else:
            out_dir_vec, z, z_mu, z_log_var = self.s2ag_generator(pre_seq, in_text, in_audio, vid_indices)

        # make a video
        assert not make_video or (make_video and target_seq is not None), \
            'target_seq cannot be None when make_video is True'
        assert not make_video or (make_video and words is not None), \
            'words cannot be None when make_video is True'
        assert not make_video or (make_video and aux_info is not None), \
            'aux_info cannot be None when make_video is True'
        assert not make_video or (make_video and save_path is not None), \
            'save_path cannot be None when make_video is True'
        if make_video:
            sentence_words = []
            for word in words:
                sentence_words.append(word)
            sentences = [' '.join(sentence_word) for sentence_word in sentence_words]

            num_videos = len(aux_info)
            for vid_idx in range(num_videos):
                start_time = time.time()
                filename_prefix = '{}_{}'.format(aux_info[vid_idx]['vid'], vid_idx)
                filename_prefix_for_video = filename_prefix
                aux_str = '({}, time: {}-{})'.format(aux_info[vid_idx]['vid'],
                                                     str(datetime.timedelta(
                                                         seconds=aux_info[vid_idx]['start_time'])),
                                                     str(datetime.timedelta(
                                                         seconds=aux_info[vid_idx]['end_time'])))
                create_video_and_save(
                    save_path, 0, filename_prefix_for_video, 0,
                    target_seq[vid_idx].cpu().numpy(),
                    out_dir_vec_trimodal[vid_idx].cpu().numpy(), out_dir_vec[vid_idx].cpu().numpy(),
                    np.reshape(self.s2ag_config_args.mean_dir_vec, -1), sentences[vid_idx],
                    audio=in_audio[vid_idx].cpu().numpy(), aux_str=aux_str,
                    clipping_to_shortest_stream=True, delete_audio_file=False)
                print('\rRendered {} of {} videos. Last one took {:.2f} seconds.'.format(vid_idx + 1,
                                                                                         num_videos,
                                                                                         time.time() - start_time),
                      end='')
            print()

        # calculate metrics
        assert not calculate_metrics or (calculate_metrics and target_seq is not None), \
            'target_seq cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and losses_all_trimodal is not None), \
            'losses_all_trimodal cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and joint_mae_trimodal is not None), \
            'joint_mae_trimodal cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and accel_trimodal is not None), \
            'accel_trimodal cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and losses_all is not None), \
            'losses_all cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and joint_mae is not None), \
            'joint_mae cannot be None when calculate_metrics is True'
        assert not calculate_metrics or (calculate_metrics and accel is not None), \
            'accel cannot be None when calculate_metrics is True'
        if calculate_metrics:
            self.evaluator_trimodal, losses_all_trimodal, joint_mae_trimodal, accel_trimodal =\
                Processor.push_samples(self.evaluator_trimodal, target_seq, out_dir_vec_trimodal,
                                       in_text, in_audio, losses_all_trimodal, joint_mae_trimodal, accel_trimodal,
                                       self.s2ag_config_args.mean_dir_vec, self.s2ag_config_args.n_poses,
                                       self.s2ag_config_args.n_pre_poses)
            self.evaluator, losses_all, joint_mae, accel =\
                Processor.push_samples(self.evaluator, target_seq, out_dir_vec,
                                       in_text, in_audio, losses_all, joint_mae, accel,
                                       self.s2ag_config_args.mean_dir_vec, self.s2ag_config_args.n_poses,
                                       self.s2ag_config_args.n_pre_poses)

        # loss
        beta = 0.1
        huber_loss = F.smooth_l1_loss(out_dir_vec / beta, target_poses / beta) * beta
        dis_output = self.s2ag_discriminator(out_dir_vec, in_text)
        gen_error = -torch.mean(torch.log(dis_output + 1e-8))
        kld = div_reg = None

        if (self.s2ag_config_args.z_type == 'speaker' or self.s2ag_config_args.z_type == 'random') and \
                self.s2ag_config_args.loss_reg_weight > 0.0:
            if self.s2ag_config_args.z_type == 'speaker':
                # enforcing divergent gestures btw original vid and other vid
                rand_idx = torch.randperm(vid_indices.shape[0])
                rand_vids = vid_indices[rand_idx]
            else:
                rand_vids = None

            if self.use_mfcc:
                out_dir_vec_rand_vid, z_rand_vid, _, _ = self.s2ag_generator(pre_seq, in_text, in_mfcc, rand_vids)
            else:
                out_dir_vec_rand_vid, z_rand_vid, _, _ = self.s2ag_generator(pre_seq, in_text, in_audio, rand_vids)
            beta = 0.05
            pose_l1 = F.smooth_l1_loss(out_dir_vec / beta, out_dir_vec_rand_vid.detach() / beta,
                                       reduction='none') * beta
            pose_l1 = pose_l1.sum(dim=1).sum(dim=1)

            pose_l1 = pose_l1.view(pose_l1.shape[0], -1).mean(1)
            z_l1 = F.l1_loss(z.detach(), z_rand_vid.detach(), reduction='none')
            z_l1 = z_l1.view(z_l1.shape[0], -1).mean(1)
            div_reg = -(pose_l1 / (z_l1 + 1.0e-5))
            div_reg = torch.clamp(div_reg, min=-1000)
            div_reg = div_reg.mean()

            if self.s2ag_config_args.z_type == 'speaker':
                # speaker embedding KLD
                kld = -0.5 * torch.mean(1 + z_log_var - z_mu.pow(2) - z_log_var.exp())
                loss = self.s2ag_config_args.loss_regression_weight * huber_loss + \
                       self.s2ag_config_args.loss_kld_weight * kld + \
                       self.s2ag_config_args.loss_reg_weight * div_reg
            else:
                loss = self.s2ag_config_args.loss_regression_weight * huber_loss + \
                       self.s2ag_config_args.loss_reg_weight * div_reg
        else:
            loss = self.s2ag_config_args.loss_regression_weight * huber_loss  # + var_loss

        if self.meta_info['epoch'] > warm_up_epochs:
            loss += self.s2ag_config_args.loss_gan_weight * gen_error

        if train:
            loss.backward()
            self.s2ag_gen_optimizer.step()

        loss_dict = {'loss': self.s2ag_config_args.loss_regression_weight * huber_loss.item()}
        if kld:
            loss_dict['KLD'] = self.s2ag_config_args.loss_kld_weight * kld.item()
        if div_reg:
            loss_dict['DIV_REG'] = self.s2ag_config_args.loss_reg_weight * div_reg.item()

        if self.meta_info['epoch'] > warm_up_epochs and self.s2ag_config_args.loss_gan_weight > 0.0:
            loss_dict['gen'] = self.s2ag_config_args.loss_gan_weight * gen_error.item()
            loss_dict['dis'] = dis_error.item()
        # total_loss = 0.
        # for loss in loss_dict.keys():
        #     total_loss += loss_dict[loss]
        # return loss_dict, losses_all_trimodal, joint_mae_trimodal, accel_trimodal, losses_all, joint_mae, accel
        return F.l1_loss(out_dir_vec, target_poses).item() - F.l1_loss(out_dir_vec_trimodal, target_poses).item(),\
            losses_all_trimodal, joint_mae_trimodal, accel_trimodal, losses_all, joint_mae, accel

    def per_train_epoch(self):

        self.s2ag_generator.train()
        self.s2ag_discriminator.train()
        batch_s2ag_loss = 0.
        num_batches = self.num_train_samples // self.args.batch_size + 1

        start_time = time.time()
        self.meta_info['iter'] = 0

        for extended_word_seq, vec_seq, audio,\
                mfcc_features, vid_indices in self.yield_batch(train=True):
            loss, *_ = self.forward_pass_s2ag(extended_word_seq, audio, mfcc_features,
                                              vec_seq, vid_indices, train=True)
            # Compute statistics
            batch_s2ag_loss += loss

            self.iter_info['s2ag_loss'] = loss
            self.iter_info['lr_gen'] = '{}'.format(self.lr_s2ag_gen)
            self.iter_info['lr_dis'] = '{}'.format(self.lr_s2ag_dis)
            self.show_iter_info()

            self.meta_info['iter'] += 1
            print('\riter {:>3}/{} took {:>4} seconds\t'.
                  format(self.meta_info['iter'], num_batches, int(np.ceil(time.time() - start_time))), end='')

        batch_s2ag_loss /= num_batches
        self.epoch_info['mean_s2ag_loss'] = batch_s2ag_loss

        self.show_epoch_info()
        self.io.print_timer()

        # self.adjust_lr_s2ag()

    def per_val_epoch(self):

        self.s2ag_generator.eval()
        self.s2ag_discriminator.eval()
        batch_s2ag_loss = 0.
        num_batches = self.num_val_samples // self.args.batch_size + 1

        start_time = time.time()
        self.meta_info['iter'] = 0
        for extended_word_seq, vec_seq, audio,\
                mfcc_features, vid_indices in self.yield_batch(train=False):
            with torch.no_grad():
                loss, *_ = self.forward_pass_s2ag(extended_word_seq, audio, mfcc_features,
                                                  vec_seq, vid_indices, train=False)
                # Compute statistics
                batch_s2ag_loss += loss

                self.iter_info['s2ag_loss'] = loss
                self.iter_info['lr_gen'] = '{:.6f}'.format(self.lr_s2ag_gen)
                self.iter_info['lr_dis'] = '{:.6f}'.format(self.lr_s2ag_dis)
                self.show_iter_info()

            self.meta_info['iter'] += 1
            print('\riter {:>3}/{} took {:>4} seconds\t'.
                  format(self.meta_info['iter'], num_batches, int(np.ceil(time.time() - start_time))), end='')

        batch_s2ag_loss /= num_batches
        self.epoch_info['mean_s2ag_loss'] = batch_s2ag_loss
        if self.epoch_info['mean_s2ag_loss'] < self.best_s2ag_loss and \
                self.meta_info['epoch'] > self.min_train_epochs:
            self.best_s2ag_loss = self.epoch_info['mean_s2ag_loss']
            self.best_s2ag_loss_epoch = self.meta_info['epoch']
            self.s2ag_loss_updated = True
        else:
            self.s2ag_loss_updated = False

        self.show_epoch_info()
        self.io.print_timer()

    def train(self):
        trimodal_checkpoint = torch.load(jn(self.base_path, 'outputs', 'trimodal_gen.pth.tar'))
        self.trimodal_generator.load_state_dict(trimodal_checkpoint['trimodal_gen_dict'])

        if self.args.s2ag_load_last_best:
            s2ag_model_found = self.load_model_at_epoch(epoch=self.args.s2ag_start_epoch)
            if not s2ag_model_found and self.args.s2ag_start_epoch is not 'best':
                print('Warning! Trying to load best known model for s2ag: '.format(self.args.s2ag_start_epoch),
                      end='')
                s2ag_model_found = self.load_model_at_epoch(epoch='best')
                self.args.s2ag_start_epoch = self.best_s2ag_loss_epoch if s2ag_model_found else 0
                print('loaded.')
                if not s2ag_model_found:
                    print('Warning! Starting at epoch 0')
                    self.args.s2ag_start_epoch = 0
        else:
            self.args.s2ag_start_epoch = 0
        for epoch in range(self.args.s2ag_start_epoch, self.args.s2ag_num_epoch):
            self.meta_info['epoch'] = epoch

            # training
            self.io.print_log('s2ag training epoch: {}'.format(epoch))
            self.per_train_epoch()
            self.io.print_log('Done.')

            # validation
            if (epoch % self.args.val_interval == 0) or (
                    epoch + 1 == self.args.num_epoch):
                self.io.print_log('s2ag val epoch: {}'.format(epoch))
                self.per_val_epoch()
                self.io.print_log('Done.')

            # save model and weights
            if self.s2ag_loss_updated or (epoch % self.args.save_interval == 0 and epoch > self.min_train_epochs):
                torch.save({'gen_model_dict': self.s2ag_generator.state_dict(),
                            'dis_model_dict': self.s2ag_discriminator.state_dict()},
                           jn(self.args.work_dir_s2ag, 'epoch_{:06d}_loss_{:.4f}_model.pth.tar'.
                              format(epoch, self.epoch_info['mean_s2ag_loss'])))

    def generate_gestures(self, samples_to_generate=10, randomized=True, load_saved_model=True,
                          s2ag_epoch='best', make_video=False, calculate_metrics=True):

        if load_saved_model:
            s2ag_model_found = self.load_model_at_epoch(epoch=s2ag_epoch)
            assert s2ag_model_found, print('Speech to emotive gestures model not found')
            trimodal_checkpoint = torch.load(jn(self.base_path, 'outputs', 'trimodal_gen.pth.tar'))
            self.trimodal_generator.load_state_dict(trimodal_checkpoint['trimodal_gen_dict'])

        self.trimodal_generator.eval()
        self.s2ag_generator.eval()
        self.s2ag_discriminator.eval()
        batch_size = 2048

        losses_all_trimodal = AverageMeter('loss')
        joint_mae_trimodal = AverageMeter('mae_on_joint')
        accel_trimodal = AverageMeter('accel')

        losses_all = AverageMeter('loss')
        joint_mae = AverageMeter('mae_on_joint')
        accel = AverageMeter('accel')

        start_time = time.time()
        for sample_idx in np.arange(0, samples_to_generate, batch_size):
            samples_curr = np.arange(sample_idx, sample_idx + min(batch_size, samples_to_generate - sample_idx))
            words, aux_info, word_seq_tensor, word_seq_lengths, extended_word_seq, \
                pose_seq, vec_seq, target_seq, audio, spectrogram, mfcc_features, vid_indices = \
                self.return_batch(samples_curr, randomized=randomized)
            with torch.no_grad():
                loss_dict, losses_all_trimodal, joint_mae_trimodal,\
                    accel_trimodal, losses_all, joint_mae, accel = \
                    self.forward_pass_s2ag(extended_word_seq, audio, mfcc_features,
                                           vec_seq, vid_indices, train=False,
                                           target_seq=target_seq, words=words, aux_info=aux_info,
                                           save_path=self.args.video_save_path,
                                           make_video=make_video, calculate_metrics=calculate_metrics,
                                           losses_all_trimodal=losses_all_trimodal,
                                           joint_mae_trimodal=joint_mae_trimodal, accel_trimodal=accel_trimodal,
                                           losses_all=losses_all, joint_mae=joint_mae, accel=accel)
                end_idx = min(samples_to_generate, sample_idx + batch_size)

        # print metrics
        loss_dict = {'loss_trimodal': losses_all_trimodal.avg, 'joint_mae_trimodal': joint_mae_trimodal.avg,
                     'loss': losses_all.avg, 'joint_mae': joint_mae.avg}
        elapsed_time = time.time() - start_time
        if self.evaluator_trimodal and self.evaluator_trimodal.get_no_of_samples() > 0:
            frechet_dist_trimodal, feat_dist_trimodal = self.evaluator_trimodal.get_scores()
            print('[VAL Trimodal]\tloss: {:.3f}, joint mae: {:.5f}, accel diff: {:.5f},'
                  'FGD: {:.3f}, feat_D: {:.3f} / {:.1f}s'.format(losses_all_trimodal.avg,
                                                                 joint_mae_trimodal.avg, accel_trimodal.avg,
                                                                 frechet_dist_trimodal, feat_dist_trimodal,
                                                                 elapsed_time))
            loss_dict['frechet_trimodal'] = frechet_dist_trimodal
            loss_dict['feat_dist_trimodal'] = feat_dist_trimodal
        else:
            print('[VAL Trimodal]\tloss: {:.3f}, joint mae: {:.3f} / {:.1f}s'.format(losses_all_trimodal.avg,
                                                                                     joint_mae_trimodal.avg,
                                                                                     elapsed_time))

        if self.evaluator and self.evaluator.get_no_of_samples() > 0:
            frechet_dist, feat_dist = self.evaluator.get_scores()
            print('[VAL Ours]\t\tloss: {:.3f}, joint mae: {:.5f}, accel diff: {:.5f},'
                  'FGD: {:.3f}, feat_D: {:.3f} / {:.1f}s'.format(losses_all.avg, joint_mae.avg, accel.avg,
                                                                 frechet_dist, feat_dist, elapsed_time))
            loss_dict['frechet'] = frechet_dist
            loss_dict['feat_dist'] = feat_dist
        else:
            print('[VAL Ours]\t\tloss: {:.3f}, joint mae: {:.3f} / {:.1f}s'.format(losses_all.avg,
                                                                                   joint_mae.avg,
                                                                                   elapsed_time))
        end_time = time.time()
        print('Total time taken: {:.2f} seconds.'.format(end_time - start_time))

    def render_clip(self, data_params, vid_name, sample_idx, samples_to_generate,
                    clip_poses, clip_audio, sample_rate, clip_words, clip_time,
                    clip_idx=0, unit_time=None, speaker_vid_idx=0, check_duration=True,
                    fade_out=False, make_video=False, save_pkl=False):
        start_time = time.time()
        mean_dir_vec = np.squeeze(np.array(self.s2ag_config_args.mean_dir_vec))

        clip_poses_resampled = resample_pose_seq(clip_poses, clip_time[1] - clip_time[0],
                                                 self.s2ag_config_args.motion_resampling_framerate)
        target_dir_vec = convert_pose_seq_to_dir_vec(clip_poses_resampled)
        target_dir_vec = target_dir_vec.reshape(target_dir_vec.shape[0], -1)
        target_dir_vec -= mean_dir_vec
        n_frames_total = len(target_dir_vec)

        # check duration
        if check_duration:
            clip_duration = clip_time[1] - clip_time[0]
            if clip_duration < data_params['clip_duration_range'][0] or \
                    clip_duration > data_params['clip_duration_range'][1]:
                return None, None, None

        # synthesize
        for selected_vi in range(len(clip_words)):  # make start time of input text zero
            clip_words[selected_vi][1] -= clip_time[0]  # start time
            clip_words[selected_vi][2] -= clip_time[0]  # end time

        out_list_trimodal = []
        out_list = []
        n_frames = self.s2ag_config_args.n_poses
        clip_length = len(clip_audio) / sample_rate
        seed_seq = target_dir_vec[0:self.s2ag_config_args.n_pre_poses]

        # pre seq
        pre_seq_trimodal = torch.zeros((1, n_frames, self.pose_dim + 1))
        if seed_seq is not None:
            pre_seq_trimodal[0, 0:self.s2ag_config_args.n_pre_poses, :-1] = \
                torch.Tensor(seed_seq[0:self.s2ag_config_args.n_pre_poses])
            # indicating bit for seed poses
            pre_seq_trimodal[0, 0:self.s2ag_config_args.n_pre_poses, -1] = 1

        pre_seq = torch.zeros((1, n_frames, self.pose_dim + 1))
        if seed_seq is not None:
            pre_seq[0, 0:self.s2ag_config_args.n_pre_poses, :-1] = \
                torch.Tensor(seed_seq[0:self.s2ag_config_args.n_pre_poses])
            # indicating bit for seed poses
            pre_seq[0, 0:self.s2ag_config_args.n_pre_poses, -1] = 1

        # target seq
        target_seq = torch.from_numpy(target_dir_vec[0:n_frames]).unsqueeze(0).float().to(self.device)

        spectrogram = None

        # divide into synthesize units and do synthesize
        if unit_time is None:
            unit_time = self.s2ag_config_args.n_poses / \
                self.s2ag_config_args.motion_resampling_framerate
        stride_time = (self.s2ag_config_args.n_poses - self.s2ag_config_args.n_pre_poses) / \
            self.s2ag_config_args.motion_resampling_framerate
        if clip_length < unit_time:
            num_subdivisions = 1
        else:
            num_subdivisions = math.ceil((clip_length - unit_time) / stride_time)
        spectrogram_sample_length = int(round(unit_time * sample_rate / 512))
        audio_sample_length = int(unit_time * sample_rate)
        end_padding_duration = 0

        # prepare speaker input
        if self.s2ag_config_args.z_type == 'speaker':
            if speaker_vid_idx is None:
                speaker_vid_idx = np.random.randint(0, self.s2ag_generator.z_obj.n_words)
            print('vid idx:', speaker_vid_idx)
            speaker_vid_idx = torch.LongTensor([speaker_vid_idx]).to(self.device)
        else:
            speaker_vid_idx = None

        print('Sample {} of {}'.format(sample_idx + 1, samples_to_generate))
        print('Subdivisions\t|\tUnit Time\t|\tClip Length\t|\tStride Time\t|\tAudio Sample Length')
        print('{}\t\t\t\t|\t{:.4f}\t\t|\t{:.4f}\t\t|\t{:.4f}\t\t|\t{}'.
              format(num_subdivisions, unit_time, clip_length,
                     stride_time, audio_sample_length))

        out_dir_vec_trimodal = None
        out_dir_vec = None
        for sub_div_idx in range(0, num_subdivisions):
            sub_div_start_time = sub_div_idx * stride_time
            sub_div_end_time = sub_div_start_time + unit_time

            # prepare spectrogram input
            in_spec = None

            # prepare audio input
            audio_start = math.floor(sub_div_start_time / clip_length * len(clip_audio))
            audio_end = audio_start + audio_sample_length
            in_audio_np = clip_audio[audio_start:audio_end]
            if len(in_audio_np) < audio_sample_length:
                if sub_div_idx == num_subdivisions - 1:
                    end_padding_duration = audio_sample_length - len(in_audio_np)
                in_audio_np = np.pad(in_audio_np, (0, audio_sample_length - len(in_audio_np)),
                                     'constant')
            in_mfcc = torch.from_numpy(
                cmn.get_mfcc_features(in_audio_np, sr=sample_rate,
                                      num_mfcc=self.data_loader['train_data_s2ag'].num_mfcc if self.args.train_s2ag else
                                               self.data_loader['test_data_s2ag'].num_mfcc)).unsqueeze(0).to(self.device).float()
            in_audio = torch.from_numpy(in_audio_np).unsqueeze(0).to(self.device).float()

            # prepare text input
            word_seq = DataPreprocessor.get_words_in_time_range(word_list=clip_words,
                                                                start_time=sub_div_start_time,
                                                                end_time=sub_div_end_time)
            extended_word_indices = np.zeros(n_frames)  # zero is the index of padding token
            word_indices = np.zeros(len(word_seq) + 2)
            word_indices[0] = self.lang_model.SOS_token
            word_indices[-1] = self.lang_model.EOS_token
            frame_duration = (sub_div_end_time - sub_div_start_time) / n_frames
            print('Subdivision {} of {}. Words: '.format(sub_div_idx + 1, num_subdivisions), end='')
            for w_i, word in enumerate(word_seq):
                print(word[0], end=', ')
                idx = max(0, int(np.floor((word[1] - sub_div_start_time) / frame_duration)))
                extended_word_indices[idx] = self.lang_model.get_word_index(word[0])
                word_indices[w_i + 1] = self.lang_model.get_word_index(word[0])
            print('\b\b', end='.\n')
            in_text_padded = torch.LongTensor(extended_word_indices).unsqueeze(0).to(self.device)

            # prepare target seq and pre seq
            if sub_div_idx > 0:
                target_seq = torch.zeros_like(out_dir_vec)
                start_idx = n_frames * (sub_div_idx - 1)
                end_idx = min(n_frames_total, n_frames * sub_div_idx)
                target_seq[0, :(end_idx - start_idx)] = torch.from_numpy(
                    target_dir_vec[start_idx:end_idx]) \
                    .unsqueeze(0).float().to(self.device)

                pre_seq_trimodal[0, 0:self.s2ag_config_args.n_pre_poses, :-1] = \
                    out_dir_vec_trimodal.squeeze(0)[-self.s2ag_config_args.n_pre_poses:]
                # indicating bit for constraints
                pre_seq_trimodal[0, 0:self.s2ag_config_args.n_pre_poses, -1] = 1

                pre_seq[0, 0:self.s2ag_config_args.n_pre_poses, :-1] = \
                    out_dir_vec.squeeze(0)[-self.s2ag_config_args.n_pre_poses:]
                # indicating bit for constraints
                pre_seq[0, 0:self.s2ag_config_args.n_pre_poses, -1] = 1

            pre_seq_trimodal = pre_seq_trimodal.float().to(self.device)
            pre_seq = pre_seq.float().to(self.device)

            out_dir_vec_trimodal, *_ = self.trimodal_generator(pre_seq_trimodal,
                                                               in_text_padded, in_audio, speaker_vid_idx)
            out_dir_vec, *_ = self.s2ag_generator(pre_seq, in_text_padded, in_mfcc, speaker_vid_idx)

            out_seq_trimodal = out_dir_vec_trimodal[0, :, :].data.cpu().numpy()
            out_seq = out_dir_vec[0, :, :].data.cpu().numpy()

            # smoothing motion transition
            if len(out_list_trimodal) > 0:
                last_poses = out_list_trimodal[-1][-self.s2ag_config_args.n_pre_poses:]
                # delete last 4 frames
                out_list_trimodal[-1] = out_list_trimodal[-1][:-self.s2ag_config_args.n_pre_poses]

                for j in range(len(last_poses)):
                    n = len(last_poses)
                    prev_pose = last_poses[j]
                    next_pose = out_seq_trimodal[j]
                    out_seq_trimodal[j] = prev_pose * (n - j) / (n + 1) + next_pose * (j + 1) / (n + 1)

            out_list_trimodal.append(out_seq_trimodal)

            if len(out_list) > 0:
                last_poses = out_list[-1][-self.s2ag_config_args.n_pre_poses:]
                # delete last 4 frames
                out_list[-1] = out_list[-1][:-self.s2ag_config_args.n_pre_poses]

                for j in range(len(last_poses)):
                    n = len(last_poses)
                    prev_pose = last_poses[j]
                    next_pose = out_seq[j]
                    out_seq[j] = prev_pose * (n - j) / (n + 1) + next_pose * (j + 1) / (n + 1)

            out_list.append(out_seq)

        # aggregate results
        out_dir_vec_trimodal = np.vstack(out_list_trimodal)
        out_dir_vec = np.vstack(out_list)

        # fade out to the mean pose
        if fade_out:
            n_smooth = self.s2ag_config_args.n_pre_poses
            start_frame = len(out_dir_vec_trimodal) - \
                          int(end_padding_duration / data_params['audio_sr']
                              * self.s2ag_config_args.motion_resampling_framerate)
            end_frame = start_frame + n_smooth * 2
            if len(out_dir_vec_trimodal) < end_frame:
                out_dir_vec_trimodal = np.pad(out_dir_vec_trimodal,
                                              [(0, end_frame - len(out_dir_vec_trimodal)), (0, 0)],
                                              mode='constant')
            out_dir_vec_trimodal[end_frame - n_smooth:] = \
                np.zeros(self.pose_dim)  # fade out to mean poses

            n_smooth = self.s2ag_config_args.n_pre_poses
            start_frame = len(out_dir_vec) - \
                          int(end_padding_duration /
                              data_params['audio_sr'] * self.s2ag_config_args.motion_resampling_framerate)
            end_frame = start_frame + n_smooth * 2
            if len(out_dir_vec) < end_frame:
                out_dir_vec = np.pad(out_dir_vec, [(0, end_frame - len(out_dir_vec)), (0, 0)],
                                     mode='constant')
            out_dir_vec[end_frame - n_smooth:] = \
                np.zeros(self.pose_dim)  # fade out to mean poses

            # interpolation
            y_trimodal = out_dir_vec_trimodal[start_frame:end_frame]
            y = out_dir_vec[start_frame:end_frame]
            x = np.array(range(0, y.shape[0]))
            w = np.ones(len(y))
            w[0] = 5
            w[-1] = 5

            co_effs_trimodal = np.polyfit(x, y_trimodal, 2, w=w)
            fit_functions_trimodal = [np.poly1d(co_effs_trimodal[:, k])
                                      for k in range(0, y_trimodal.shape[1])]
            interpolated_y_trimodal = [fit_functions_trimodal[k](x)
                                       for k in range(0, y_trimodal.shape[1])]
            # (num_frames x dims)
            interpolated_y_trimodal = np.transpose(np.asarray(interpolated_y_trimodal))

            co_effs = np.polyfit(x, y, 2, w=w)
            fit_functions = [np.poly1d(co_effs[:, k]) for k in range(0, y.shape[1])]
            interpolated_y = [fit_functions[k](x) for k in range(0, y.shape[1])]
            # (num_frames x dims)
            interpolated_y = np.transpose(np.asarray(interpolated_y))

            out_dir_vec_trimodal[start_frame:end_frame] = interpolated_y_trimodal
            out_dir_vec[start_frame:end_frame] = interpolated_y

        filename_prefix = '{}_{}_{}'.format(vid_name, speaker_vid_idx, clip_idx)
        filename_prefix_for_save = filename_prefix.split('_tensor')[0]
        sentence_words = []
        for word, _, _ in clip_words:
            sentence_words.append(word)
        sentence = ' '.join(sentence_words)

        # make a video
        if make_video:
            aux_str = '({}, time: {}-{})'.format(vid_name,
                                                 str(datetime.timedelta(seconds=clip_time[0])),
                                                 str(datetime.timedelta(seconds=clip_time[1])))
            create_video_and_save(
                self.args.video_save_path, self.best_s2ag_loss_epoch, filename_prefix_for_save, 0, target_dir_vec,
                out_dir_vec_trimodal, out_dir_vec, mean_dir_vec, sentence,
                audio=clip_audio, aux_str=aux_str, clipping_to_shortest_stream=True,
                delete_audio_file=False)
            print('Rendered {} of {} videos. Last one took {:.2f} seconds.'.
                  format(sample_idx + 1, samples_to_generate, time.time() - start_time))

        out_dir_vec_trimodal = out_dir_vec_trimodal + mean_dir_vec
        out_poses_trimodal = convert_dir_vec_to_pose(out_dir_vec_trimodal)
        out_dir_vec = out_dir_vec + mean_dir_vec
        out_poses = convert_dir_vec_to_pose(out_dir_vec)

        # save pkl
        if save_pkl:
            save_dict = {
                'sentence': sentence, 'audio': clip_audio.astype(np.float32),
                'out_dir_vec': out_dir_vec_trimodal, 'out_poses': out_poses_trimodal,
                'aux_info': '{}_{}_{}'.format(vid_name, speaker_vid_idx, clip_idx),
                'human_dir_vec': target_dir_vec + mean_dir_vec,
            }
            with open(jn(self.args.video_save_path,
                         '{}_trimodal.pkl'.format(filename_prefix_for_save)), 'wb') as f:
                pickle.dump(save_dict, f)

            save_dict = {
                'sentence': sentence, 'audio': clip_audio.astype(np.float32),
                'out_dir_vec': out_dir_vec, 'out_poses': out_poses,
                'aux_info': '{}_{}_{}'.format(vid_name, speaker_vid_idx, clip_idx),
                'human_dir_vec': target_dir_vec + mean_dir_vec,
            }
            with open(jn(self.args.video_save_path,
                         '{}.pkl'.format(filename_prefix)), 'wb') as f:
                pickle.dump(save_dict, f)

        return clip_poses_resampled, out_poses_trimodal, out_poses

    def generate_gestures_by_dataset(self, dataset, data_params, check_duration=True,
                                     randomized=True, fade_out=False,
                                     load_saved_model=True, s2ag_epoch='best',
                                     make_video=False, save_pkl=False):

        if load_saved_model:
            s2ag_model_found = self.load_model_at_epoch(epoch=s2ag_epoch)
            assert s2ag_model_found, print('Speech to emotive gestures model not found')
            trimodal_checkpoint = torch.load(jn(self.base_path, 'outputs', 'trimodal_gen.pth.tar'))
            self.trimodal_generator.load_state_dict(trimodal_checkpoint['trimodal_gen_dict'])

        self.trimodal_generator.eval()
        self.s2ag_generator.eval()
        self.s2ag_discriminator.eval()

        overall_start_time = time.time()

        if dataset.lower() == 'ted_db':
            if 'clip_duration_range' not in data_params.keys():
                data_params['clip_duration_range'] = [5, 12]
            lmdb_env = lmdb.open(data_params['env_file'], readonly=True, lock=False)
            with lmdb_env.begin(write=False) as txn:
                keys = [key for key, _ in txn.cursor()]
                samples_to_generate = len(keys)
                print('Total samples to generate: {}'.format(samples_to_generate))
                for sample_idx in range(samples_to_generate):  # loop until we get the desired number of results
                    # select video
                    if randomized:
                        key = np.random.choice(keys)
                    else:
                        key = keys[sample_idx]
                    buf = txn.get(key)
                    video = pyarrow.deserialize(buf)
                    vid_name = video['vid']
                    clips = video['clips']
                    n_clips = len(clips)
                    if n_clips == 0:
                        continue
                    if randomized:
                        clip_idx = np.random.randint(0, n_clips)
                        speaker_vid_idx = np.random.randint(0, self.test_speaker_model.n_words)
                    else:
                        clip_idx = 0
                        speaker_vid_idx = 0

                    clip_poses = clips[clip_idx]['skeletons_3d']
                    clip_audio = clips[clip_idx]['audio_raw']
                    clip_words = clips[clip_idx]['words']
                    clip_time = [clips[clip_idx]['start_time'], clips[clip_idx]['end_time']]
                    clip_poses_resampled, out_poses_trimodal, out_poses =\
                        self.render_clip(data_params, vid_name, sample_idx,
                                         samples_to_generate, clip_poses, clip_audio,
                                         data_params['audio_sr'], clip_words, clip_time,
                                         clip_idx=clip_idx, speaker_vid_idx=speaker_vid_idx,
                                         check_duration=check_duration, fade_out=fade_out,
                                         make_video=make_video, save_pkl=save_pkl)

        elif dataset.lower() == 'genea_challenge_2020':
            file_names = ['.wav'.join(f.split('.wav')[:-1]) for f in os.listdir(jn(data_params['data_path'], 'audio'))]
            file_names.sort()

            samples_to_generate = len(file_names)
            print('Total samples to generate: {}'.format(samples_to_generate))
            joint_indices_to_keep = [0, 4, 6, 7, 9, 10, 11, 28, 29, 30]
            for f_idx, f in enumerate(file_names):
                audio, sample_rate = librosa.load(jn(data_params['data_path'], 'audio', f + '.wav'),
                                                  mono=True, sr=16000, res_type='kaiser_fast')
                j_names, _, _, joint_positions, _, frame_rate =\
                    MoCapDataset.load_bvh(jn(data_params['data_path'], 'bvh_raw', f + '.bvh'))
                joint_positions_max = np.power(10., np.ceil(np.log10(np.max(joint_positions))))
                joint_positions_min = np.min(joint_positions)
                if joint_positions_min >= 0:
                    joint_positions_min = 0.
                else:
                    joint_positions_min = -np.power(10., np.ceil(np.log10(np.abs(joint_positions_min))))
                joint_positions_scaled =\
                    2. * (joint_positions - joint_positions_min) / (joint_positions_max - joint_positions_min) - 1.
                with open(jn(data_params['data_path'], 'transcripts', f + '.json'), 'r') as jf:
                    data_dump = json.load(jf)
                    transcript = []
                    for json_data in data_dump:
                        words_with_timings = json_data['alternatives'][0]['words']
                        for word in words_with_timings:
                            transcript.append([word['word'],
                                               float(word['start_time'][:-1]), float(word['end_time'][:-1])])
                clip_time = [0., len(joint_positions) / np.round(frame_rate)]
                if randomized:
                    speaker_vid_idx = np.random.randint(0, self.test_speaker_model.n_words)
                else:
                    speaker_vid_idx = 0

                clip_poses_resampled, out_poses_trimodal, out_poses =\
                    self.render_clip(data_params, f, f_idx, samples_to_generate,
                                     joint_positions_scaled[:, joint_indices_to_keep],
                                     audio, sample_rate, transcript, clip_time,
                                     clip_idx=0, speaker_vid_idx=speaker_vid_idx,
                                     check_duration=check_duration, fade_out=fade_out,
                                     make_video=make_video, save_pkl=save_pkl)

        end_time = time.time()
        print('Total time taken: {:.2f} seconds.'.format(end_time - overall_start_time))
