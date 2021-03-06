import torch.utils.data as data # 加载torch的数据加载器
import numpy as np
import time
import cv2
import sys
import os
sys.path.append(os.getcwd())
import argparse
import model.model as crnn
import torch
import torch.optim as optim

from utils.utils import load_yml,model_info,get_batch_label,get_optimizer,encode,decode
from data.dataset import OCRDataset

def parse_arg():
    parser = argparse.ArgumentParser(description="train crnn")
    parser.add_argument('--cfg', help='experiment configuration filename', required=True, type=str)
    args = parser.parse_args()
    config = load_yml(args.cfg)
    return config

if __name__ == "__main__":
    config = parse_arg()
    print(config)
    if not os.path.exists(config.OUTPUT_DIR):
        os.mkdir(config.OUTPUT_DIR)
    train_dataset = OCRDataset(config)
    train_loader = data.DataLoader(
        dataset=train_dataset,
        batch_size=config.TRAIN.BATCH_SIZE_PER_GPU,
        shuffle=config.TRAIN.SHUFFLE,
        num_workers=config.WORKERS,
        pin_memory=config.PIN_MEMORY,
    )
    val_dataset = OCRDataset(config,is_train=False)
    val_loader = data.DataLoader(
        dataset=val_dataset,
        batch_size=config.TEST.BATCH_SIZE_PER_GPU,
        shuffle=config.TEST.SHUFFLE,
        num_workers=config.WORKERS,
        pin_memory=config.PIN_MEMORY,
    )
    # get device
    if torch.cuda.is_available():
        device = torch.device("cuda:{}".format(config.GPUID))
    else:
        device = torch.device("cpu:0")

    model = crnn.get_crnn(config)
    model = model.to(device)
    model_info(model)
    print(model)


    optimizer = get_optimizer(config, model)
    last_epoch = config.TRAIN.BEGIN_EPOCH
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, config.TRAIN.LR_STEP,
            config.TRAIN.LR_FACTOR, last_epoch-1
        )
    if config.ATTENTION.ENABLE:
        criterion = torch.nn.NLLLoss()
    else:
        criterion = torch.nn.CTCLoss()
    # 训练
    best_acc = 0.0
    for epoch in range(last_epoch,config.TRAIN.END_EPOCH):
      model.train()
      for i, (inp, idx) in enumerate(train_loader):
          # 前馈,计算loss
          inp = inp.to(device)
          labels = get_batch_label(train_dataset, idx)
          batch_size = inp.size(0)
          text, length = encode(config.DICT,labels)
          preds = model(inp,length).cpu()
          preds_size = torch.IntTensor([preds.size(0)] * batch_size)
          if config.ATTENTION.ENABLE:
              loss = criterion(preds, text)
          else:
              loss = criterion(preds, text, preds_size, length)

          # 反馈
          optimizer.zero_grad()
          loss.backward()
          optimizer.step()
          if i % config.PRINT_FREQ == 0:
            print("epoch:{} step:{} loss:{} lr:{}".format(epoch,i,loss.item(),lr_scheduler.get_lr()))
      # 每个epoch更新学习率
      lr_scheduler.step()

      # 每EVAL_FREQ评估一次并保存best模型
      if epoch % config.EVAL_FREQ == 0:
          model.eval()
          n_correct = 0
          test_num = len(val_loader) * config.TEST.BATCH_SIZE_PER_GPU
          with torch.no_grad():
              for i, (inp, idx) in enumerate(val_loader):
                  # 计算前馈,计算loss
                  inp = inp.to(device)
                  labels = get_batch_label(val_dataset, idx)
                  batch_size = inp.size(0)
                  text, length = encode(config.DICT,labels)
                  preds = model(inp,length).cpu()
                  preds_size = torch.IntTensor([preds.size(0)] * batch_size)
                  if config.ATTENTION.ENABLE:
                      loss = criterion(preds, text)
                  else:
                      loss = criterion(preds, text, preds_size, length)

                  # 后处理解码
                  if config.ATTENTION.ENABLE:
                      _, preds = preds.max(1)
                      index = 0
                      for label in labels:
                          pre = "".join(str(preds[index:index + len(label)]))
                          index += len(label)
                          if pre == label:
                              n_correct += 1
                  else:
                      _, preds = preds.max(2)
                      preds = preds.transpose(1, 0).contiguous().view(-1)
                      sim_preds = decode(preds.data, preds_size.data, config.DICT,raw=False)
                      for pred, target in zip(sim_preds, labels):
                        if pred == target:
                          n_correct += 1

              
          # 抓一个batch来显示
          if not config.ATTENTION.ENABLE:
              raw_preds = decode(preds.data, preds_size.data, config.DICT, raw=True)[:config.TEST.NUM_TEST_DISP]
              for raw_pred, pred, gt in zip(raw_preds, sim_preds, labels):
                  print('%-20s => %-20s, gt: %-20s' % (raw_pred, pred, gt))
          now_acc = n_correct * 1.0 / test_num
          print("best_acc:{} correct:{} total:{}".format(now_acc,n_correct,test_num))
          if now_acc >= best_acc:
              torch.save(
                        {
                            "state_dict": model.state_dict(),
                            "epoch": epoch + 1,
                            "best_acc": best_acc,
                        },  os.path.join(config.OUTPUT_DIR, "checkpoint_{}_acc_{:.4f}.pth".format(epoch, now_acc)))
              best_acc = now_acc
              print("save_model!")
