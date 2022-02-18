import sys
import os
import math

import numpy as np
from numpy.random import shuffle
import scipy
import pandas as pd

from typing import Tuple

import torch
import torch.nn.functional as F
import torch.nn as nn
import torch.optim as optim

from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import Sampler, SequentialSampler
from torch.backends import cudnn

from tqdm import tqdm
import matplotlib.pyplot as plt
import seaborn as sns

import wandb

device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


hyperparameter_defaults = dict(
    c_1=128,
    c_2=64,
    c_3=32,
    c_4=16,
    d_1=128,
    d_2=64,
    d_3=32,
    learning_rate=4e-3,
    weight_decay=1e-2,
)

wandb.init(config=hyperparameter_defaults, project="DeepSpCas9")
config = wandb.config


class GeneInteractionModel(nn.Module):

    def __init__(self, hidden_size, num_layers):
        super(GeneInteractionModel, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        self.c1 = nn.Sequential(
            nn.Conv2d(in_channels=4, out_channels=config.c_1, kernel_size=(2, 3), stride=1, padding=(0, 1)),
            nn.GELU(),
        )
        
        self.c2 = nn.Sequential(
            nn.Conv1d(in_channels=config.c_1, out_channels=config.c_2, kernel_size=3, stride=1, padding=1),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),

            nn.Conv1d(in_channels=config.c_2, out_channels=config.c_2, kernel_size=3, stride=1, padding=1),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),

            nn.Conv1d(in_channels=config.c_2, out_channels=config.c_3, kernel_size=3, stride=1, padding=1),
            nn.GELU(),
            nn.AvgPool1d(kernel_size=2, stride=2),
        )

        self.r = nn.GRU(config.c_3, hidden_size, num_layers,
                        batch_first=True, bidirectional=True)

        self.s = nn.Linear(2 * hidden_size, config.c_4, bias=False)
        
        self.d = nn.Sequential(
            nn.Linear(27, config.d_1, bias=False),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(config.d_1, config.d_2, bias=False), 
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(config.d_2, config.d_3, bias=False)
        )

        self.head = nn.Sequential(
            # nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(config.c_4 + config.d_3, 1, bias=True),
        )
        
    def forward(self, g, x):
        g = torch.squeeze(self.c1(g), 2)
        g = self.c2(g)
        g, _ = self.r(torch.transpose(g, 1, 2))
        g = self.s(g[:, -1, :])
        
        x = self.d(x)

        out = self.head(torch.cat((g, x), dim=1))

        return out


class GeneFeatureDataset(Dataset):
    
    def __init__(
        self,
        gene: torch.Tensor = None,
        features: torch.Tensor = None,
        target: torch.Tensor = None,
        fold: int = None,
        mode: str = 'train',
        fold_list: np.ndarray = None,
    ):
        self.fold = fold
        self.mode = mode
        self.fold_list = fold_list
        
        if self.fold_list is not None:
            self.indices = self._select_fold()
            self.gene = gene[self.indices]
            self.features = features[self.indices]
            self.target = target[self.indices]
        else:
            self.gene = gene
            self.features = features
            self.target = target

    def _select_fold(self):
        selected_indices = []
        
        if self.mode == 'valid': # SELECT A SINGLE GROUP
            for i in range(len(self.fold_list)):
                if self.fold_list[i] == self.fold:
                    selected_indices.append(i)
        elif self.mode == 'train': # SELECT OTHERS
            for i in range(len(self.fold_list)):
                if self.fold_list[i] != self.fold:
                    selected_indices.append(i)
        else: # FOR FINALIZING
            for i in range(len(self.fold_list)):
                selected_indices.append(i)

        return selected_indices
    
    def __len__(self):
        return len(self.gene)
    
    def __getitem__(self, idx:int) -> Tuple[torch.Tensor, torch.Tensor]:
        gene = self.gene[idx]
        features = self.features[idx]
        target = self.target[idx]

        return gene, features, target


def preprocess_seq(data):
    print("Start preprocessing the sequence done 2d")
    length = 74

    DATA_X = np.zeros((len(data), 1, length, 4), dtype=float)
    print(np.shape(data), len(data), length)
    for l in tqdm(range(len(data))):
        for i in range(length):

            try:
                data[l][i]
            except:
                print(data[l], i, length, len(data))

            if data[l][i] in "Aa":
                DATA_X[l, 0, i, 0] = 1
            elif data[l][i] in "Cc":
                DATA_X[l, 0, i, 1] = 1
            elif data[l][i] in "Gg":
                DATA_X[l, 0, i, 2] = 1
            elif data[l][i] in "Tt":
                DATA_X[l, 0, i, 3] = 1
            elif data[l][i] in "Xx":
                pass
            else:
                print("Non-ATGC character " + data[l])
                print(i)
                print(data[l][i])
                sys.exit()

    print("Preprocessed the sequence")
    return DATA_X


def seq_concat(data):
    wt = preprocess_seq(data.WT74_On)
    ed = preprocess_seq(data.Edited74_On)
    g = np.concatenate((wt, ed), axis=1)
    g = 2 * g - 1

    return g


# LOAD DATA
train_PECV = pd.read_csv('data/DeepPrime_PECV__train_220214.csv')
test_PECV = pd.read_csv('data/DeepPrime_PECV__test_220214.csv')


# PREPROCESS GENES

if not os.path.isfile('data/g_train.npy'):
    g_train = seq_concat(train_PECV)
    np.save('data/g_train.npy', g_train)
else:
    g_train = np.load('data/g_train.npy')

if not os.path.isfile('data/g_test.npy'):
    g_test = seq_concat(test_PECV)
    np.save('data/g_test.npy', g_test)
else:
    g_test = np.load('data/g_test.npy')


# FEATURE SELECTION

train_features = train_PECV.loc[:, ['PBSlen', 'RTlen', 'RT-PBSlen', 'Edit_pos', 'Edit_len', 'RHA_len', 'type_sub',
                                    'type_ins', 'type_del', 'Tm1', 'Tm2', 'Tm2new', 'Tm3', 'Tm4', 'TmD',
                                    'nGCcnt1', 'nGCcnt2', 'nGCcnt3', 'fGCcont1', 'fGCcont2', 'fGCcont3',
                                    'MFE1', 'MFE2', 'MFE3', 'MFE4', 'MFE5', 'DeepSpCas9_score']]
train_fold = train_PECV.Fold
train_target = train_PECV.Measured_PE_efficiency

test_features = test_PECV.loc[:, ['PBSlen', 'RTlen', 'RT-PBSlen', 'Edit_pos', 'Edit_len', 'RHA_len', 'type_sub',
                                    'type_ins', 'type_del', 'Tm1', 'Tm2', 'Tm2new', 'Tm3', 'Tm4', 'TmD',
                                    'nGCcnt1', 'nGCcnt2', 'nGCcnt3', 'fGCcont1', 'fGCcont2', 'fGCcont3',
                                    'MFE1', 'MFE2', 'MFE3', 'MFE4', 'MFE5', 'DeepSpCas9_score']]
test_target = test_PECV.Measured_PE_efficiency


# NORMALIZATION

x_train = (train_features - train_features.mean()) / train_features.std()
y_train = (train_target - train_target.mean()) / train_target.std()
x_train = x_train.to_numpy()
y_train = y_train.to_numpy()

x_test = (test_features - train_features.mean()) / train_features.std()
y_test = (test_target - train_target.mean()) / train_target.std()
x_test = x_test.to_numpy()
y_test = y_test.to_numpy()

g_train = torch.tensor(g_train, dtype=torch.float32, device=device)
x_train = torch.tensor(x_train, dtype=torch.float32, device=device)
y_train = torch.tensor(y_train, dtype=torch.float32, device=device)

g_test = torch.tensor(g_test, dtype=torch.float32, device=device)
x_test = torch.tensor(x_test, dtype=torch.float32, device=device)
y_test = torch.tensor(y_test, dtype=torch.float32, device=device)


# PARAMS

batch_size = 2048
learning_rate = config.learning_rate
weight_decay = config.weight_decay
T_0 = 12
T_mult = 1
hidden_size = 128
n_layers = 1
n_epochs = 12
n_models = 1


# TRAINING & VALIDATION

for m in range(n_models):

    random_seed = m

    torch.manual_seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)
    np.random.seed(random_seed)

    for fold in range(5):
        
        model = GeneInteractionModel(hidden_size=hidden_size, num_layers=n_layers).to(device)

        train_set = GeneFeatureDataset(g_train, x_train, y_train, fold, 'train', train_fold)
        valid_set = GeneFeatureDataset(g_train, x_train, y_train, fold, 'valid', train_fold)

        train_loader = DataLoader(dataset=train_set, batch_size=batch_size, shuffle=True, num_workers=0)
        valid_loader = DataLoader(dataset=valid_set, batch_size=batch_size, shuffle=True, num_workers=0)

        criterion = nn.MSELoss()
        optimizer = torch.optim.AdamW(
            model.parameters(), lr=learning_rate, weight_decay=weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, T_0=T_0, T_mult=T_mult, eta_min=learning_rate/100)

        n_iters = len(train_loader)

        for epoch in tqdm(range(n_epochs)):
            train_loss, valid_loss = [], []
            train_count, valid_count = 0, 0

            model.train()

            for i, (g, x, y) in enumerate(train_loader):
                g = g.permute((0, 3, 1, 2))
                y = y.reshape(-1, 1)

                pred = model(g, x)
                loss = criterion(pred, y)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                scheduler.step(epoch + i / n_iters)

                train_loss.append(x.size(0) * loss.detach().cpu().numpy())
                train_count += x.size(0)

            model.eval()

            pred_, y_ = None, None

            with torch.no_grad():
                for i, (g, x, y) in enumerate(valid_loader):
                    g = g.permute((0, 3, 1, 2))
                    y = y.reshape(-1, 1)

                    pred = model(g, x)
                    loss = criterion(pred, y)
                    
                    valid_loss.append(x.size(0) * loss.detach().cpu().numpy())
                    valid_count += x.size(0)

                    if pred_ is None:
                        pred_ = pred.detach().cpu().numpy()
                        y_ = y.detach().cpu().numpy()
                    else:
                        pred_ = np.concatenate((pred_, pred.detach().cpu().numpy()))
                        y_ = np.concatenate((y_, y.detach().cpu().numpy()))


            train_loss = sum(train_loss) / train_count
            valid_loss = sum(valid_loss) / valid_count

            SPR = scipy.stats.spearmanr(pred_, y_).correlation

            print('[FOLD {:02}/{:02}] [M {:03}/{:03}] [E {:03}/{:03}] : {:.4f} | {:.4f} | {:.4f}'.format(fold + 1, 5, m + 1,
                n_models, epoch + 1, n_epochs, train_loss, valid_loss, SPR))
            
            metrics = {'Train loss': train_loss, 'Valid loss': valid_loss, 'Spearman score': SPR}

            wandb.log(metrics)
        
        break
