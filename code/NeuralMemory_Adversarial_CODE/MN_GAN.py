import model_GAN
import torch
from torch.autograd import Variable
import torch.optim as optim

from sampler import WARPSampler
from dataloader import movielens
import numpy as np
from tqdm import tqdm
import pandas as pd
import pdb
import time

import os
os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"]="0"

if __name__ == '__main__':
    dim = 64
    memory_size = 128
    # 1000
    BATCH_SIZE = 1000
    # 200
    margin = 4
    use_rank_weight = True
    lr = 0.2
    user_negs_n = 5000
    n_candiidates = 50
    n_negative = 10
    topk = 30

    """
    pre training
    """
    train1_pd, test1_pd, test2_pd, test3_pd, test4_pd, most_popular_items, n_users, n_items = movielens(
        'datasets/ml/ratings.csv')

    n_users = int(n_users)
    n_items = int(n_items)

    networkD = model_GAN.Dis(dim = dim, n_users = n_users, n_items = n_items, memory_size = memory_size)
    networkG = model_GAN.Gen(dim = dim, n_users = n_users, n_items = n_items)
    networkD = networkD.cuda()
    networkG = networkG.cuda()
    optimizerD = optim.Adagrad(networkD.parameters(), lr = lr)
    optimizerG = optim.Adagrad(networkG.parameters(), lr = lr)
    # pretrain
    sampler = WARPSampler(train1_pd, most_popular_items, n_items, batch_size = BATCH_SIZE,
                          n_candiidates = n_negative,
                          check_negative = True)
    for user_pos, neg_cands in tqdm(sampler.next_batch(), desc = 'pre training',
                                    total = train1_pd.shape[0] / BATCH_SIZE):
        """ 
        Pre-training
        """
        networkD.zero_grad()
        pos_users = user_pos[:, 0].astype(int)
        pos_items = user_pos[:, 1].astype(int)
        pos_users = Variable(torch.from_numpy(pos_users)).cuda()
        pos_items = Variable(torch.from_numpy(pos_items)).cuda()
        neg_cands = Variable(torch.from_numpy(neg_cands)).cuda()
        d_loss = networkD(pos_users, pos_items, neg_cands, use_rank_weight, margin)
        d_loss.backward()
        optimizerD.step()

        networkG.zero_grad()
        D_G_p, D_G_n, _, _, _ = networkG(pos_users, pos_items, neg_cands, n_negative)
        probs = D_G_p / torch.sum(D_G_n, dim = 1, keepdim = True)
        g_loss = torch.sum(probs)
        g_loss.backward()
        optimizerG.step()
    """ 
    Adversarial and streming updating
    """
    test_pds = [test1_pd, test2_pd, test3_pd, test4_pd]
    # test_pds = [test1_pd]
    train_pd = train1_pd
    previous_test_pd = train1_pd
    for test_part, test_pd in enumerate(test_pds):
        user_to_train_set = dict()
        user_to_test_set = dict()
        train_users = train_pd['user'].values.tolist()
        train_items = train_pd['item'].values.tolist()
        all_users_in_train = set(train_users)
        all_items_in_train = set(train_items)

        for t in train_pd.itertuples():
            user_to_train_set.setdefault(t.user, set())
            user_to_train_set[t.user].add(t.item)
        for t in test_pd.itertuples():
            user_to_test_set.setdefault(t.user, set())
            user_to_test_set[t.user].add(t.item)

        sampler = WARPSampler(previous_test_pd, most_popular_items, n_items, batch_size = BATCH_SIZE,
                              n_candiidates = n_candiidates,
                              check_negative = True)
        epoch = 0
        while epoch < 10:
            epoch += 1
            for user_pos, neg_cands in tqdm(sampler.next_batch(), desc = 'epoch:{}, training'.format(epoch),
                                            total = previous_test_pd.shape[0] / BATCH_SIZE):
                networkD.zero_grad()
                pos_users = user_pos[:, 0].astype(int)
                pos_items = user_pos[:, 1].astype(int)
                pos_users = Variable(torch.from_numpy(pos_users)).cuda()
                pos_items = Variable(torch.from_numpy(pos_items)).cuda()
                neg_cands = Variable(torch.from_numpy(neg_cands)).cuda()
                _, _, _, _, negs_index = networkG(pos_users, pos_items, neg_cands, n_negative)
                # neg_item = torch.gather(neg_cands, dim = 1, index = neg_index)
                neg_items = torch.gather(neg_cands, dim = 1, index = negs_index)
                # train D
                d_loss = networkD(pos_users, pos_items, neg_items.detach(), use_rank_weight, margin)
                d_loss.backward()
                optimizerD.step()

                #  train G
                networkG.zero_grad()
                _, _, probs, neg_index, _ = networkG(pos_users, pos_items, neg_cands, n_negative)
                neg_item = torch.gather(neg_cands, dim = 1, index = neg_index)
                log_prob = torch.gather(probs, dim = 1, index = neg_index).log()
                D_D = networkD.get_D_D(pos_users, neg_item)

                g_loss = torch.sum(log_prob * D_D)
                g_loss.backward()
                optimizerG.step()
        """
        Testing
        """
        user_negative_samples = dict()
        items_set = set(list(range(1, n_items)))
        for u in tqdm(user_to_train_set.keys(), desc = 'sampling user negative items'):
            user_negative_samples[u] = np.random.choice(list(items_set - user_to_train_set[u]), user_negs_n)
        accs = []
        # all_items_embeddings = network.item_embeddings.weight
        for test_u in tqdm(list(user_to_test_set.keys()), desc = 'testing'):
            if test_u not in all_users_in_train:
                continue
            users_v = Variable(torch.from_numpy(np.array([test_u], dtype = int))).cuda()
            # [1, D]
            abst_prefers_embeds = networkD.abs_embed(users_v)
            hit = 0
            tot = 0
            for test_v in user_to_test_set[test_u]:
                if test_v not in all_items_in_train:
                    continue
                candidate_items = np.append(user_negative_samples[test_u], test_v)
                # [N, D]
                candidate_items_embeddings = networkD.item_embeddings(
                    Variable(torch.from_numpy(candidate_items)).cuda())
                item_scores = torch.sum((candidate_items_embeddings - abst_prefers_embeds) ** 2, dim = 1)
                # item_scores = item_scores.cpu().data.numpy()
                # user_tops = np.argpartition(item_scores, -topk)[-topk:]
                _, user_tops = torch.topk(item_scores, k = topk, largest = False)
                user_tops = user_tops.cpu().data.numpy()
                tot += 1
                if user_negs_n in user_tops:
                    hit += 1
            if tot > 0:
                accs.append(float(hit) / tot)
        print('Final accuracy@{} on test {} : {}'.format(topk, np.mean(accs), test_part + 1))
        previous_test_pd = test_pd
        train_pd = pd.concat([train_pd, test_pd])
