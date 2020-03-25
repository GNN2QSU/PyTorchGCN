import os
import time
import glob
import torch
import random
import numpy as np
import torch.optim as optim
from alisuretool.Tools import Tools
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader

from nets.load_net import gnn_model
from data.superpixels import SuperPixDataset
from train.train_superpixels_graph_classification import train_epoch, evaluate_network  # import train functions


def gpu_setup(use_gpu, gpu_id):
    if torch.cuda.is_available() and use_gpu:
        Tools.print('Cuda available with GPU: {}'.format(torch.cuda.get_device_name(0)))
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        device = torch.device("cuda")
    else:
        Tools.print('Cuda not available')
        device = torch.device("cpu")
    return device


def view_model_param(model):
    total_param = 0
    for param in model.parameters():
        total_param += np.prod(list(param.data.size()))
    return total_param


def set_seed(seed, device):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if device == 'cuda':
        torch.cuda.manual_seed(seed)
        pass
    pass


def save_checkpoint(model, root_ckpt_dir, epoch):
    torch.save(model.state_dict(), os.path.join(root_ckpt_dir, 'epoch_{}.pkl'.format(epoch)))
    for file in glob.glob(root_ckpt_dir + '/*.pkl'):
        if int(file.split('_')[-1].split('.')[0]) < epoch - 1:
            os.remove(file)
            pass
        pass
    pass


def train_val_pipeline(model_name, dataset, params, net_params, root_log_dir, root_ckpt_dir):

    if model_name in ['GCN', 'GAT'] and net_params['self_loop']:
        Tools.print("[!] Adding graph self-loops for GCN/GAT models (central node trick).")
        dataset.add_self_loops()
        pass

    device = net_params['device']
    set_seed(params['seed'], device=device)

    writer = SummaryWriter(log_dir=os.path.join(root_log_dir, "RUN_" + str(0)))

    Tools.print("Training Graphs: {}".format(len(dataset.train)))
    Tools.print("Validation Graphs: {}".format(len(dataset.val)))
    Tools.print("Test Graphs: {}".format(len(dataset.test)))
    Tools.print("Number of Classes: {}".format(net_params['n_classes']))

    model = gnn_model(model_name, net_params).to(device)

    optimizer = optim.Adam(model.parameters(), lr=params['init_lr'], weight_decay=params['weight_decay'])
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=params['lr_reduce_factor'],
                                                     patience=params['lr_schedule_patience'], verbose=True)

    drop_last = True if model_name == 'DiffPool' else False
    train_loader = DataLoader(dataset.train, batch_size=params['batch_size'],
                              shuffle=True, drop_last=drop_last, collate_fn=dataset.collate)
    val_loader = DataLoader(dataset.val, batch_size=params['batch_size'],
                            shuffle=False, drop_last=drop_last, collate_fn=dataset.collate)
    test_loader = DataLoader(dataset.test, batch_size=params['batch_size'],
                             shuffle=False, drop_last=drop_last, collate_fn=dataset.collate)

    Tools.print()
    net_params['total_param'] = view_model_param(model)
    Tools.print("Dataset: {}, Model: {}\nparams={}\nnet_params={}".format(dataset.name, model_name, params, net_params))
    Tools.print()

    t0, per_epoch_time = time.time(), []
    epoch_train_losses, epoch_val_losses, epoch_train_accs, epoch_val_accs = [], [], [], []
    for epoch in range(params['epochs']):
        start = time.time()
        Tools.print()
        Tools.print("Start Epoch {}".format(epoch))

        epoch_train_loss, epoch_train_acc, optimizer = train_epoch(model, optimizer, device, train_loader)
        epoch_val_loss, epoch_val_acc = evaluate_network(model, device, val_loader)
        epoch_test_loss, epoch_test_acc = evaluate_network(model, device, test_loader)

        scheduler.step(epoch_val_loss)
        save_checkpoint(model, root_ckpt_dir, epoch)

        epoch_train_losses.append(epoch_train_loss)
        epoch_val_losses.append(epoch_val_loss)
        epoch_train_accs.append(epoch_train_acc)
        epoch_val_accs.append(epoch_val_acc)

        writer.add_scalar('train/_loss', epoch_train_loss, epoch)
        writer.add_scalar('val/_loss', epoch_val_loss, epoch)
        writer.add_scalar('train/_acc', epoch_train_acc, epoch)
        writer.add_scalar('val/_acc', epoch_val_acc, epoch)
        writer.add_scalar('learning_rate', optimizer.param_groups[0]['lr'], epoch)

        per_epoch_time.append(time.time() - start)
        Tools.print("time={:.4f}, lr={:.4f}, loss={:.4f}/{:.4f}/{:.4f}, acc={:.4f}/{:.4f}/{:.4f}".format(
            time.time() - start, optimizer.param_groups[0]['lr'], epoch_train_loss,
            epoch_val_loss, epoch_test_loss, epoch_train_acc, epoch_val_acc, epoch_test_acc))

        # Stop training
        if optimizer.param_groups[0]['lr'] < params['min_lr']:
            Tools.print("\n!! LR EQUAL TO MIN LR SET.")
            break
        if time.time() - t0 > params['max_time'] * 3600:
            Tools.print("Max_time for training elapsed {:.2f} hours, so stopping".format(params['max_time']))
            break

        pass

    _, test_acc = evaluate_network(model, device, test_loader)
    _, train_acc = evaluate_network(model, device, train_loader)
    Tools.print("Test Accuracy: {:.4f}".format(test_acc))
    Tools.print("Train Accuracy: {:.4f}".format(train_acc))
    Tools.print("TOTAL TIME TAKEN: {:.4f}s".format(time.time() - t0))
    Tools.print("AVG TIME PER EPOCH: {:.4f}s".format(np.mean(per_epoch_time)))

    writer.close()
    pass


def main(out_dir, data_file, dataset_name="MNIST", model_name="GCN", use_gpu=False, gpu_id="0", batch_size=128):
    device = gpu_setup(use_gpu=use_gpu, gpu_id=gpu_id)
    dataset = SuperPixDataset(dataset_name, data_file=data_file)  # MNIST or CIFAR10
    num_classes = len(np.unique(np.array(dataset.train[:][1])))

    # parameters
    params = {
        "seed": 41,
        "epochs": 1000,
        "batch_size": batch_size,
        "init_lr": 0.001,
        "lr_reduce_factor": 0.5,
        "lr_schedule_patience": 5,
        "min_lr": 1e-5,
        "weight_decay": 0.0,
        "print_epoch_interval": 5,
        "max_time": 48
    }
    net_params = {
        "batch_size": batch_size,
        "L": 4,
        "hidden_dim": 146,
        "out_dim": 146,
        "residual": True,
        "readout": "mean",
        "in_feat_dropout": 0.0,
        "dropout": 0.0,
        "graph_norm": True,
        "batch_norm": True,
        "self_loop": False,
        'in_dim': dataset.train[0][0].ndata['feat'][0].size(0),
        'in_dim_edge': dataset.train[0][0].edata['feat'][0].size(0),
        'n_classes': num_classes,
        'device': device,
        'gpu_id': gpu_id
    }

    if model_name == 'DiffPool':
        max_num_nodes_train = max([dataset.train[i][0].number_of_nodes() for i in range(len(dataset.train))])
        max_num_nodes_test = max([dataset.test[i][0].number_of_nodes() for i in range(len(dataset.test))])
        max_num_node = max(max_num_nodes_train, max_num_nodes_test)
        net_params['assign_dim'] = int(max_num_node * net_params['pool_ratio']) * net_params['batch_size']
        pass

    file_name = "{}_{}_GPU{}_{}".format(model_name, dataset_name, gpu_id, time.strftime('%Hh%Mm%Ss_on_%b_%d_%Y'))
    root_log_dir = Tools.new_dir("{}/logs/{}".format(out_dir, file_name))
    root_ckpt_dir = Tools.new_dir("{}/checkpoints/{}".format(out_dir, file_name))

    train_val_pipeline(model_name, dataset, params, net_params, root_log_dir, root_ckpt_dir)
    pass


if __name__ == '__main__':
    main(out_dir=Tools.new_dir("result/m1_demo"), data_file="/mnt/4T/ALISURE/GCN/MNIST.pkl")
