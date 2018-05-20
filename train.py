import argparse
import logging
import sys

import numpy as np
from libs.data import VOCdataset
from libs.net import Darknet_19
from torchvision import transforms
from torch.optim.lr_scheduler import MultiStepLR

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pdb
import os


parser = argparse.ArgumentParser(description='PyTorch YOLOv2')
parser.add_argument('--anchor_scales', type=str,
                    default=('1.3221,1.73145,'
                             '3.19275,4.00944,'
                             '5.05587,8.09892,'
                             '9.47112,4.84053,'
                             '11.2364,10.0071'),
                    help='anchor scales')
parser.add_argument('--resume', type=str, default=None,
                    help='path to latest checkpoint')
parser.add_argument('--start_epoch', default=0, type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--epochs', type=int, default=160,
                    help='number of total epochs to run')
parser.add_argument('--lr', type=float, default=0.001,
                    help='base learning rate')
parser.add_argument('--num_classes', type=int, default=20,
                    help='number of classes')
parser.add_argument('--num_anchors', type=int, default=5,
                    help='number of anchors per cell')
parser.add_argument('--weight_decay', type=float, default=0.0005,
                    help='weight of l2 regularize')
parser.add_argument('--batch_size', type=int, default=16,
                    help='batch size must be 1')
parser.add_argument('--iou_obj', type=float, default=2.236,
                    help='iou loss weight')
parser.add_argument('--iou_noobj', type=float, default=1.0,
                    help='iou loss weight')
parser.add_argument('--coord_obj', type=float, default=1.0,
                    help='coord loss weight with obj')
parser.add_argument('--prob_obj', type=float, default=1.0,
                    help='prob loss weight with obj')
parser.add_argument('--coord_noobj', type=float, default=0.1,
                    help='coord loss weight without obj')
parser.add_argument('--pretrained_model', type=str, default=None,
                    help='path to pretrained model')


logger = logging.getLogger()
fmt = logging.Formatter('%(asctime)s %(levelname)-8s: %(message)s')
file_handler = logging.FileHandler('train.log')
file_handler.setFormatter(fmt)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(fmt)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)


def variable_input_collate_fn(batch):
    data = list(zip(*batch))
    return [torch.stack(data[0], 0), data[1]]


def iou(anchors, gt, h, w):
    anchors_xmax = anchors[..., 0]+0.5*anchors[..., 2]
    anchors_xmin = anchors[..., 0]-0.5*anchors[..., 2]
    anchors_ymax = anchors[..., 1]+0.5*anchors[..., 3]
    anchors_ymin = anchors[..., 1]-0.5*anchors[..., 3]

    # clip value to (0, w/h)
    np.clip(anchors_xmax, 0, w, out=anchors_xmax)
    np.clip(anchors_xmin, 0, w, out=anchors_xmin)
    np.clip(anchors_ymax, 0, h, out=anchors_ymax)
    np.clip(anchors_ymin, 0, h, out=anchors_ymin)

    tb = np.minimum(anchors_xmax, gt[0]+0.5*gt[2])-np.maximum(anchors_xmin, gt[0]-0.5*gt[2])
    lr = np.minimum(anchors_ymax, gt[1]+0.5*gt[3])-np.maximum(anchors_ymin, gt[1]-0.5*gt[3])
    intersection = tb * lr
    intersection[np.where((tb < 0) | (lr < 0))] = 0
    return intersection / (anchors[..., 2]*anchors[..., 3] + gt[2]*gt[3] - intersection)


def build_target(out_shape, gt, anchor_scales, seen, threshold=0.6):
    bs, h, w, n, _ = out_shape

    target_bbox = np.zeros((bs, h, w, n, 4), dtype=np.float32)
    prob_mask = np.zeros((bs, h, w, n, 1), dtype=np.float32)
    iou_mask = np.ones((bs, h, w, n), dtype=np.float32)
    target_iou = np.zeros((bs, h, w, n), dtype=np.float32)
    anchors = np.zeros((h, w, n, 4), dtype=np.float32)

    if seen < 12800:
        bbox_mask = np.tile(args.coord_noobj, (bs, h, w, n, 1)).astype(np.float32)
        target_bbox[..., 0:2].fill(0.5)
    else:
        bbox_mask  = np.zeros((bs, h, w, n, 1), dtype=np.float32)

    target_class = np.zeros((bs, h, w, n, args.num_classes), dtype=np.float32)

    anchors[..., 0] += np.arange(0.5, w, 1).reshape(1, w, 1)
    anchors[..., 1] += np.arange(0.5, h, 1).reshape(h, 1, 1)
    anchors[..., 2:] += anchor_scales

    for b in range(bs):
        num_gts = len(gt[b])
        for i in range(num_gts):
            gt_x = (gt[b][i][0]+gt[b][i][2])/2
            gt_y = (gt[b][i][1]+gt[b][i][3])/2
            gt_w = gt[b][i][2]-gt[b][i][0]
            gt_h = gt[b][i][3]-gt[b][i][1]
            gt_x, gt_y, gt_w, gt_h = gt_x*w, gt_y*h, gt_w*w, gt_h*h

            ious = iou(anchors, np.array([gt_x, gt_y, gt_w, gt_h], dtype=np.float32), h, w)
            flatten_idxs = np.argmax(ious)
            multidim_idxs = np.unravel_index(flatten_idxs, (h, w, n))

            bbox_mask[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = args.coord_obj
            prob_mask[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = args.prob_obj
            target_iou[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = ious[multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]]

            # an anchor with any ground_truth's iou > threshold and is not the best match then ignore it
            iou_mask[b][np.where((ious <= threshold) &
                                 (iou_mask[b] != args.iou_obj) &
                                 (iou_mask[b] != 0))] = args.iou_noobj
            
            iou_mask[b][np.where((ious > threshold) & 
                                 (iou_mask[b] != args.iou_obj))] = 0
            iou_mask[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = args.iou_obj

            tx, ty = gt_x-np.floor(gt_x), gt_y-np.floor(gt_y)
            tw = np.log(gt_w/anchor_scales[multidim_idxs[2]][0])
            th = np.log(gt_h/anchor_scales[multidim_idxs[2]][1])
            target_bbox[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = tx, ty, tw, th
            target_class[b, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2], int(gt[b][i][4])] = 1

    return bbox_mask, prob_mask, iou_mask, target_bbox, target_class, target_iou


def save_fn(state, filename='./yolov2.pth.tar'):
    torch.save(state, filename)


def train(train_loader, model, anchor_scales, epochs, opt):
    lr_scheduler = MultiStepLR(opt, milestones=[60, 90], gamma=0.1)
    samples = len(train_loader)
    criterion = nn.MSELoss(size_average=False)
    model.train()
    seen = 0
    for epoch in range(args.start_epoch, epochs):
        lr_scheduler.step(epoch=epoch)
        bbox_loss_avg, prob_loss_avg, iou_loss_avg = 0.0, 0.0, 0.0

        for idx, (imgs, labels) in enumerate(train_loader):
            imgs = imgs.cuda()
            opt.zero_grad()
            with torch.enable_grad():
                bbox_pred, iou_pred, prob_pred = model(imgs)
            
            bbox_mask, prob_mask, iou_mask, target_bbox, target_class, target_iou = \
                build_target(bbox_pred.size(), labels, anchor_scales, seen)
            
            bbox_mask = torch.from_numpy(bbox_mask).cuda()
            prob_mask = torch.from_numpy(prob_mask).cuda()
            iou_mask = torch.from_numpy(iou_mask).cuda()            
            target_bbox = torch.from_numpy(target_bbox).cuda()
            target_class = torch.from_numpy(target_class).cuda()
            target_iou = torch.from_numpy(target_iou).cuda()

            num_gts = sum(len(gts) for gts in labels)

            with torch.enable_grad():
                bbox_loss = criterion(bbox_pred*bbox_mask, target_bbox*bbox_mask) / num_gts
                prob_loss = criterion(prob_pred*prob_mask, target_class*prob_mask) / num_gts
                iou_loss = criterion(iou_pred*iou_mask, target_iou*iou_mask) / num_gts
                loss = bbox_loss+prob_loss+iou_loss
            loss.backward()
            opt.step()
            bbox_loss_avg += bbox_loss.item()
            prob_loss_avg += prob_loss.item()
            iou_loss_avg += iou_loss.item()
            seen += args.batch_size
            # if idx % 10 == 0:
            #     logger.info('epoch:{} step:{} bbox loss:{} probs loss:{} iou loss:{}'.format(
            #         epoch, idx, bbox_loss.item(), prob_loss.item(), iou_loss.item()))
        logger.info('epoch: {}  bbox loss: {}  probs loss: {}  iou loss: {}'.format(
            epoch, bbox_loss_avg/samples, prob_loss_avg/samples, iou_loss_avg/samples
        ))
        save_fn({'epoch': epoch+1,
                 'state_dict': model.state_dict(),
                 'optimizer': opt.state_dict()})


def main():
    global args
    args = parser.parse_args()
    # assert args.batch_size == 1
    anchor_scales = map(float, args.anchor_scales.split(','))
    anchor_scales = np.array(list(anchor_scales)).reshape(-1, 2)

    data_transform = transforms.Compose(
            [
                transforms.Resize((416, 416)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
    train_dataset = VOCdataset(usage='train', transform=data_transform)
    train_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=args.batch_size,
                                               shuffle=True,
                                               num_workers=4,
                                               pin_memory=True,
                                               collate_fn=variable_input_collate_fn,
                                               drop_last=True)

    darknet = Darknet_19(3, args.num_anchors, args.num_classes)
    darknet.load_from_npz(args.pretrained_model, num_conv=18)
    darknet.cuda()
    optimizer = optim.SGD(darknet.parameters(),
                          lr=args.lr,
                          weight_decay=args.weight_decay)

    if args.resume:
        if os.path.isfile(args.resume):
            print("load checkpoint from '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            darknet.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("loaded checkpoint '{}' (epoch {})".format(
                args.resume, checkpoint['epoch']))
        else:
            print("no checkpoint found at '{}'".format(args.resume))
    train(train_loader,
          darknet,
          anchor_scales,
          epochs=args.epochs,
          opt=optimizer)


if __name__ == '__main__':
    main()