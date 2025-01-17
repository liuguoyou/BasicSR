import os.path
import sys
import math
import argparse
import time
import random
import numpy as np
from collections import OrderedDict
import logging

import torch

import options.options as option
from utils import util
from data import create_dataloader, create_dataset
from models import create_model


def get_pytorch_ver():
    #print(torch.__version__)
    pytorch_ver = torch.__version__
    if pytorch_ver == "0.4.0":
        return "pre"
    elif pytorch_ver == "0.4.1":
        return "pre"
    elif pytorch_ver == "1.0.0":
        return "pre"
    else: #"1.1.0", "1.1.1", "1.2.0", "1.2.1" and beyond
        return "post"

def main():
    # options
    parser = argparse.ArgumentParser()
    parser.add_argument('-opt', type=str, required=True, help='Path to option JSON file.')
    opt = option.parse(parser.parse_args().opt, is_train=True)
    opt = option.dict_to_nonedict(opt)  # Convert to NoneDict, which return None for missing key.
    pytorch_ver = get_pytorch_ver()
    
    # train from scratch OR resume training
    if opt['path']['resume_state']:
        if os.path.isdir(opt['path']['resume_state']):
            import glob
            resume_state_path = util.sorted_nicely(glob.glob(os.path.normpath(opt['path']['resume_state']) + '/*.state'))[-1]
        else:
            resume_state_path = opt['path']['resume_state']
        resume_state = torch.load(resume_state_path)
    else:  # training from scratch
        resume_state = None
        util.mkdir_and_rename(opt['path']['experiments_root'])  # rename old folder if exists
        util.mkdirs((path for key, path in opt['path'].items() if not key == 'experiments_root'
                     and 'pretrain_model' not in key and 'resume' not in key))
    
    # config loggers. Before it, the log will not work
    util.setup_logger(None, opt['path']['log'], 'train', level=logging.INFO, screen=True)
    util.setup_logger('val', opt['path']['log'], 'val', level=logging.INFO)
    logger = logging.getLogger('base')
    
    if resume_state:
        logger.info('Set [resume_state] to ' + resume_state_path)
        logger.info('Resuming training from epoch: {}, iter: {}.'.format(
            resume_state['epoch'], resume_state['iter']))
        option.check_resume(opt)  # check resume options

    logger.info(option.dict2str(opt))
    # tensorboard logger
    if opt['use_tb_logger'] and 'debug' not in opt['name']:
        from tensorboardX import SummaryWriter
        try:
            tb_logger = SummaryWriter(logdir='../tb_logger/' + opt['name']) #for version tensorboardX >= 1.7
        except:
            tb_logger = SummaryWriter(log_dir='../tb_logger/' + opt['name']) #for version tensorboardX < 1.6

    # random seed
    seed = opt['train']['manual_seed']
    if seed is None:
        seed = random.randint(1, 10000)
    logger.info('Random seed: {}'.format(seed))
    util.set_random_seed(seed)

    torch.backends.cudnn.benckmark = True
    # torch.backends.cudnn.deterministic = True

    # create train and val dataloader
    for phase, dataset_opt in opt['datasets'].items():
        if phase == 'train':
            train_set = create_dataset(dataset_opt)
            train_size = int(math.ceil(len(train_set) / dataset_opt['batch_size']))
            logger.info('Number of train images: {:,d}, iters: {:,d}'.format(
                len(train_set), train_size))
            total_iters = int(opt['train']['niter'])
            total_epochs = int(math.ceil(total_iters / train_size))
            logger.info('Total epochs needed: {:d} for iters {:,d}'.format(
                total_epochs, total_iters))
            train_loader = create_dataloader(train_set, dataset_opt)
        elif phase == 'val':
            val_set = create_dataset(dataset_opt)
            val_loader = create_dataloader(val_set, dataset_opt)
            logger.info('Number of val images in [{:s}]: {:d}'.format(dataset_opt['name'],
                                                                      len(val_set)))
        else:
            raise NotImplementedError('Phase [{:s}] is not recognized.'.format(phase))
    assert train_loader is not None

    # create model
    model = create_model(opt)

    # resume training
    if resume_state:
        start_epoch = resume_state['epoch']
        current_step = resume_state['iter']
        model.resume_training(resume_state)  # handle optimizers and schedulers
        model.update_schedulers(opt['train']) # updated schedulers in case JSON configuration has changed
    else:
        current_step = 0
        start_epoch = 0

    # training
    logger.info('Start training from epoch: {:d}, iter: {:d}'.format(start_epoch, current_step))
    for epoch in range(start_epoch, total_epochs):
        for n, train_data in enumerate(train_loader,start=1):
            current_step += 1
            if current_step > total_iters:
                break
            
            if pytorch_ver=="pre": #Order for PyTorch ver < 1.1.0
                # update learning rate
                model.update_learning_rate(current_step-1)
                # training
                model.feed_data(train_data)
                model.optimize_parameters(current_step)
            elif pytorch_ver=="post": #Order for PyTorch ver > 1.1.0
                # training
                model.feed_data(train_data)
                model.optimize_parameters(current_step)
                # update learning rate
                model.update_learning_rate(current_step-1)
            else:
                print('Error identifying PyTorch version. ', torch.__version__)
                break

            # log
            if current_step % opt['logger']['print_freq'] == 0:
                logs = model.get_current_log()
                message = '<epoch:{:3d}, iter:{:8,d}, lr:{:.3e}> '.format(
                    epoch, current_step, model.get_current_learning_rate())
                for k, v in logs.items():
                    message += '{:s}: {:.4e} '.format(k, v)
                    # tensorboard logger
                    if opt['use_tb_logger'] and 'debug' not in opt['name']:
                        tb_logger.add_scalar(k, v, current_step)
                logger.info(message)

            # save models and training states (changed to save models before validation)
            if current_step % opt['logger']['save_checkpoint_freq'] == 0:
                model.save(current_step)
                model.save_training_state(epoch + (n >= len(train_loader)), current_step)
                logger.info('Models and training states saved.')
            
            # validation
            if current_step % opt['train']['val_freq'] == 0:
                avg_psnr_c = 0.0
                avg_psnr_s = 0.0
                avg_psnr_p = 0.0
                idx = 0
                for val_data in val_loader:
                    idx += 1
                    img_name = os.path.splitext(os.path.basename(val_data['LR_path'][0]))[0]
                    img_dir = os.path.join(opt['path']['val_images'], img_name)
                    util.mkdir(img_dir)

                    model.feed_data(val_data)
                    model.test()

                    visuals = model.get_current_visuals()
                    img_c = util.tensor2img(visuals['img_c'])  # uint8
                    img_s = util.tensor2img(visuals['img_s'])  # uint8
                    img_p = util.tensor2img(visuals['img_p'])  # uint8
                    
                    gt_img = util.tensor2img(visuals['HR'])  # uint8

                    # Save SR images for reference
                    save_c_img_path = os.path.join(img_dir, '{:s}_{:d}_c.png'.format(img_name, current_step))
                    save_s_img_path = os.path.join(img_dir, '{:s}_{:d}_s.png'.format(img_name, current_step))
                    save_p_img_path = os.path.join(img_dir, '{:s}_{:d}_d.png'.format(img_name, current_step))
                    
                    util.save_img(img_c, save_c_img_path)
                    util.save_img(img_s, save_s_img_path)
                    util.save_img(img_p, save_p_img_path)

                    # calculate PSNR
                    crop_size = opt['scale']
                    gt_img = gt_img / 255.
                    #sr_img = sr_img / 255. #ESRGAN 
                    #PPON
                    #C
                    sr_img_c = img_c / 255.
                    cropped_sr_img_c = sr_img_c[crop_size:-crop_size, crop_size:-crop_size, :]
                    cropped_gt_img = gt_img[crop_size:-crop_size, crop_size:-crop_size, :]
                    avg_psnr_c += util.calculate_psnr(cropped_sr_img_c * 255, cropped_gt_img * 255)
                    #S
                    sr_img_s = img_s / 255.
                    cropped_sr_img_s = sr_img_s[crop_size:-crop_size, crop_size:-crop_size, :]
                    avg_psnr_s += util.calculate_psnr(cropped_sr_img_s * 255, cropped_gt_img * 255)
                    #D
                    sr_img_p = img_p / 255.
                    cropped_sr_img_p = sr_img_p[crop_size:-crop_size, crop_size:-crop_size, :]
                    avg_psnr_p += util.calculate_psnr(cropped_sr_img_p * 255, cropped_gt_img * 255)
                    

                avg_psnr_c = avg_psnr_c / idx
                avg_psnr_s = avg_psnr_s / idx
                avg_psnr_p = avg_psnr_p / idx

                # log
                logger.info('# Validation # PSNR_c: {:.5g}'.format(avg_psnr_c))
                logger.info('# Validation # PSNR_s: {:.5g}'.format(avg_psnr_s))
                logger.info('# Validation # PSNR_p: {:.5g}'.format(avg_psnr_p))
                logger_val = logging.getLogger('val')  # validation logger
                logger_val.info('<epoch:{:3d}, iter:{:8,d}> psnr_c: {:.5g}, psnr_s: {:.5g}, psnr_p: {:.5g}'.format(
                    epoch, current_step, avg_psnr_c, avg_psnr_s, avg_psnr_p))
                # tensorboard logger
                if opt['use_tb_logger'] and 'debug' not in opt['name']:
                    tb_logger.add_scalar('psnr_c', avg_psnr_c, current_step)
                    tb_logger.add_scalar('psnr_s', avg_psnr_s, current_step)
                    tb_logger.add_scalar('psnr_p', avg_psnr_p, current_step)


    logger.info('Saving the final model.')
    model.save('latest')
    logger.info('End of training.')


if __name__ == '__main__':
    main()
