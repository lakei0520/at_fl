import os, argparse
import numpy as np
from tqdm import tqdm
from torchvision.utils import save_image
from utils import *
from inversion import RepInversion
from multiprocessing import Process, freeze_support
if __name__ == '__main__':
    # /////////////// Setup ///////////////
    # Arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--inet-dir', type=str, default='./data/')
    #数据路径
    parser.add_argument('--split', type=str, choices=['train', 'val'], default='val')
    #分割
    parser.add_argument('--expr', type=str, default='rep_recover_acc')
    #主函数选项，
    # Model options
    parser.add_argument('--arch', type=str, default='resnet50',
                        choices=['resnet18', 'resnet50', 'wide_resnet50_4', 'vgg16_bn', 'densenet161', 'resnext50_32x4d', 'mobilenet'],
                        help='Choose architecture.')
    #模型
    parser.add_argument('--eps', type=float,  default=3 , choices=[0, 0.05, 0.1, 0.25, 0.5, 1, 3])
    # Experiment setting options
    parser.add_argument('--seed', '-s', type=int, default=233, help='Random seed.')
    parser.add_argument('--batch-size', type=int, choices=[16, 64, 128, 256], default=64)
    parser.add_argument('--num-batches', type=int, default=5)
    parser.add_argument('--num-samples-per-class', type=int, default=5)
    parser.add_argument('--anchor-labels-choice', type=int, default=10)
    # Optimization options
    parser.add_argument('--opt', type=str, choices=['adam', 'sgd'], default='adam')
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--lr-decay', type=str, choices=['none', 'cosine'], default='none')
    parser.add_argument('--steps', type=int, default=5000)
    parser.add_argument('--bn-reg', type=float, default=0)
    parser.add_argument('--tv-l1-reg', type=float, default=0)
    parser.add_argument('--tv-l2-reg', type=float, default=1e-6)
    parser.add_argument('--l2-reg', type=float, default=0)
    parser.add_argument('--jitter', action='store_true')
    parser.add_argument('--jitter-lim', type=int, default=32)
    parser.add_argument('--flipping', action='store_true')
    parser.add_argument('--noise-step', action='store_true')
    parser.add_argument('--noise-scale', type=float, default=0.2)
    parser.add_argument('--restarts', type=int, default=5)
    parser.add_argument('--use-best', action='store_true')
    # Acceleration
    parser.add_argument('--gpu', nargs='*', type=int, default=[0,1])
    parser.add_argument('--prefetch', type=int, default=4, help='Pre-fetching threads.')
    args = parser.parse_args()


    # Set up GPU
    os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(map(lambda x: str(x), args.gpu))

    transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        # normalization is part of the model forward pass
        #transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])

    dset = ImageFolder(os.path.join(args.inet_dir, args.split), transform)

    # load model
    net = get_model(arch=args.arch, eps=args.eps, checkpoint_dir="./pretrained-models", inet_dir=args.inet_dir)


    if args.expr == 'rep_recover_acc':
        # randomly pick samples from each class
        label_2_index = get_inet_label_index_pair(args.inet_dir, args.split)
        rng = np.random.RandomState(args.seed)
        anchor_labels = list(range(args.anchor_labels_choice))
        anchor_indices_list = [list(rng.choice(label_2_index[i], args.num_samples_per_class, replace=False)) for i in anchor_labels]

        all_results = []
        all_true_rep = []
        all_recov_rep = []
        for anchor_label, anchor_indices in tqdm(enumerate(anchor_indices_list), total=len(anchor_indices_list), leave=True, position=2, desc='Classes'):
            level_1 = []
            tr_level_1 = []
            rr_level_1 = []
            for anchor_index in tqdm(anchor_indices, total=len(anchor_indices), leave=False, position=1, desc='Samples'):
                batch_index_list, anchor_position_list = get_batch_index(anchor_label, anchor_index, label_2_index, batch_size=args.batch_size, num_batches=args.num_batches)

                all_batches = [i for b in batch_index_list for i in b]
                dataloader = DataLoader(
                    Subset(dset, all_batches),
                    batch_size=args.batch_size,
                    shuffle=False,
                    num_workers=4,
                )

                level_2 = []
                tr_level_2 = []
                rr_level_2 = []

                gt_list = []
                for batch_data, anchor_position in tqdm(zip(dataloader, anchor_position_list), total=len(dataloader), leave=False, position=0, desc='Batches'):
                    # sanity check
                    assert batch_data[1][anchor_position] == anchor_label

                    batch_fc_grads, true_reps, gt_imgs = get_batch_fc_gradients_and_reps(batch_data, net, eps=args.eps)
                    recovered_reps = recov_reps_from_gradients(batch_fc_grads, args.batch_size)
                    level_2.append(
                        F.cosine_similarity(true_reps[anchor_position], -recovered_reps[anchor_position], dim=0).item()
                    )
                    tr_level_2.append(true_reps[anchor_position].detach().cpu())
                    rr_level_2.append(-recovered_reps[anchor_position].detach().cpu())
                    gt_list.append(gt_imgs[anchor_position].cpu())

                gt_img = gt_list[np.argmax(level_2)]
                save_folder = os.path.join(
                    'results',
                    f"recov_img_arch={args.arch}_eps={args.eps:g}_bs={args.batch_size}_samplesperclass={args.num_samples_per_class}_numbatches={args.num_batches}",
                    f"{anchor_label}_{anchor_index}_{np.max(level_2):.16f}"
                )
                if not os.path.exists(save_folder):
                    os.makedirs(save_folder)

                save_image(
                    gt_img,
                    os.path.join(save_folder, 'gt.png')
                )

                level_1.append(level_2)
                tr_level_1.append(tr_level_2[np.argmax(level_2)])
                rr_level_1.append(rr_level_2[np.argmax(level_2)])

            all_results.append(level_1)
            all_true_rep.append(tr_level_1)
            all_recov_rep.append(rr_level_1)

        if not os.path.exists('results'):
            os.makedirs('results')
        np.save(
            os.path.join('results', f"rep_acc_arch={args.arch}_eps={args.eps:g}_bs={args.batch_size}_samplesperclass={args.num_samples_per_class}_numbatches={args.num_batches}_{args.split}.npy"),
            np.array(all_results)
        )

        torch.save(
            {
                'true_rep': all_true_rep,
                'recov_rep': all_recov_rep
            },
            os.path.join('results', f"rep_arch={args.arch}_eps={args.eps:g}_bs={args.batch_size}_samplesperclass={args.num_samples_per_class}_numbatches={args.num_batches}_{args.split}.pt")
        )


    elif args.expr == 'softmax_prob_distribution':
        label_2_index = get_inet_label_index_pair(args.inet_dir, args.split)
        rng = np.random.RandomState(args.seed)
        anchor_labels = list(range(100))
        anchor_indices_list = [list(rng.choice(label_2_index[i], args.num_samples_per_class, replace=False)) for i in anchor_labels]
        anchor_indices_list = np.asarray(anchor_indices_list).flatten()

        dataloader = DataLoader(Subset(dset, anchor_indices_list), shuffle=False, batch_size=200, num_workers=4)
        save_path = os.path.join('results', f"softmax_prob_arch={args.arch}_eps={args.eps:.1f}_{args.split}.pt")

        attack_kwargs = {
            'constraint': '2', # L-2 PGD
            'eps': args.eps, # Epsilon constraint (L-2 norm)
            'step_size': args.eps * 2/3, # Learning rate for PGD
            'iterations': 3, # Number of PGD steps
            'targeted': False, # Untargeted attack
            'custom_loss': None # Use default cross-entropy loss
        }
        make_adv = args.eps > 0

        results = []
        for x, y in tqdm(dataloader, total=len(dataloader), desc='Batches', leave=True, position=0):
            x, y = x.cuda(), y.cuda()
            logits = net(x, y, make_adv, with_image=False, with_latent=False, **attack_kwargs)
            results.append(
                (F.softmax(logits, dim=-1)*F.one_hot(y, num_classes=1000)).sum(dim=-1).detach()
            )
        results = torch.cat(results).cpu()
        assert len(results) == len(anchor_indices_list)
        torch.save(results, save_path)


    elif args.expr == 'img_recover':
        temp = torch.load(
            os.path.join('results', f"rep_arch={args.arch}_eps={args.eps:.1f}_bs={args.batch_size}_samplesperclass=5_numbatches=5_val.pt")
        )
        recov_rep = []
        for x1 in temp['recov_rep']:
            for x2 in x1:
                recov_rep.append(x2)
        recov_rep = torch.stack(recov_rep)#.view(1000, 5, 5, -1)

        recov_acc = np.load(
            os.path.join('results', f"rep_acc_arch={args.arch}_eps={args.eps:.1f}_bs={args.batch_size}_samplesperclass=5_numbatches=5_val.npy")
        )

        label_2_index = get_inet_label_index_pair(args.inet_dir, args.split)
        rng = np.random.RandomState(args.seed)
        anchor_labels = list(range(1000))
        anchor_indices_list = [list(rng.choice(label_2_index[i], args.num_samples_per_class, replace=False)) for i in anchor_labels]
        anchor_indices = np.array(anchor_indices_list).flatten()

        ind_2_label = {}
        for i, a in enumerate(anchor_indices):
            ind_2_label[a] = i//5

        save_root = os.path.join(
            'results',
            f"recov_img_arch={args.arch}_eps={args.eps:.1f}_bs={args.batch_size}_samplesperclass=5_numbatches=5"
        )
        if not os.path.exists(save_root):
            os.makedirs(save_root)

        indices_dict = {
            0: {
                'resnet18': [58, 711, 4969, 155, 2, 229, 4370, 419, 586, 4984],
                'resnet50': [4946, 952, 4962, 4019, 4627, 104, 58, 409, 4370, 516],
                'wide_resnet50_4': [0, 4962, 247, 32, 485, 4950, 58, 4987, 4738, 4532],
                'vgg16_bn': [462, 101, 15, 109, 509, 142, 4969, 491, 229, 376],
                'densenet161': [4570, 104, 58, 4725, 4863, 0, 5, 711, 4089, 454]
            },
            3: {
                'resnet18': [4795, 1608, 1962, 4933, 4962, 14, 558, 475, 4918, 491],
                'resnet50': [2466, 4933, 3429, 15, 14, 638, 1613, 4969, 4962, 589],
                'wide_resnet50_4': [4933, 159, 4962, 638, 108, 4903, 4969, 4949, 14, 149],
                'vgg16_bn': [3429, 4962, 1625, 4571, 13, 4795, 1962, 1586, 4933, 1463],
                'densenet161': [4933, 558, 4570, 17, 1962, 2951, 491, 404, 13, 3429],
            }
        }

        # inverter class
        config = dict(
            # optimizer
            lr=args.lr,
            optimizer=args.opt,             # adam, sgd
            momentum=0.9,                   # only applies to sgd
            adam_betas=[0.9, 0.999],
            max_iters=args.steps,
            lr_decay=args.lr_decay,         # none, cosine
            warmup_iters=0,                 # only applies to cosine schedule
            # regularization strength
            inv_reg=1.,
            bn_reg=args.bn_reg,
            tv_l1_reg=args.tv_l1_reg,
            tv_l2_reg=args.tv_l2_reg,
            l2_reg=args.l2_reg,
            # regularization options
            jitter=args.jitter,
            jitter_lim=args.jitter_lim,
            flipping=args.flipping,
            noise_step=args.noise_step,
            noise_scale=args.noise_scale,
            restarts=args.restarts,
            # others
            print_iter=500,
            use_best=args.use_best,
            seed=args.seed,
        )
        inverter = RepInversion(config)
        invert_id = f"opt={args.opt}_lr={args.lr}_lrdecay={args.lr_decay}_iters={args.steps}"
        invert_id += f"_bn={args.bn_reg:.0e}_tvl1={args.tv_l1_reg:.0e}_tvl2={args.tv_l2_reg:.0e}_l2={args.l2_reg:.0e}"
        invert_id += "_jitter=0" if not args.jitter else f"_jitter={args.jitter_lim:d}"
        invert_id += "_noise=0" if not args.noise_step else f"_noise={args.noise_scale}"
        invert_id += f"_flipping={args.flipping}"
        invert_id += f"_restarts={args.restarts}_usebest={args.use_best}_seed={args.seed}"


        indices = indices_dict[args.eps][args.arch]
        targ_rep = recov_rep[torch.from_numpy(np.array(indices))].cuda()
        bs = targ_rep.size(0)
        gt_img_set = Subset(dset, anchor_indices[indices])

        batch_recov_acc = recov_acc.max(axis=-1).flatten()[indices]
        recov_img_optimal, recov_img_all, stats = inverter.invert(net, targ_rep, bs=bs, img_shape=(224, 224))

        for kk, (img, a, ii) in enumerate(zip(recov_img_optimal, batch_recov_acc, anchor_indices[indices])):
            tqdm.write(f"{ind_2_label[ii]}_{ii}")

            save_path = os.path.join(
                save_root, f"{ind_2_label[ii]}_{ii}_{a:.16f}", f'{invert_id}_optimal.png'
            )
            save_image(img, save_path)

        if len(recov_img_all) > 1:
            for t, recov_img in enumerate(recov_img_all):
                for kk, (img, a, ii) in enumerate(zip(recov_img, batch_recov_acc, anchor_indices[indices])):
                    save_path = os.path.join(
                        save_root, f"{ind_2_label[ii]}_{ii}_{a:.16f}", f'{invert_id}_trial{t+1}.png'
                    )
                    save_image(img, save_path)

    else:
        raise ValueError