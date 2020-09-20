In all the experiments listed below, we have at least 3 main files:

1. `run.sh` which specifies what arguments were used to run the python file
2. `train.py` which is the python file that contains the driver code to run the experiment 
3. `environment.yml` is a conda-specific environment file. It installs the
   specific version of `alr` used for that particular experiment. Note, it
   also installs the exact version of other auxiliary packages we use (e.g.
   pytorch, matplotlib, numpy, etc.)

To re-run our experiments, start by creating an exact replica of 
the conda environment using `conda env create -n <env name> -f environment.yml` for that particular experiment.
Then, run `train.py` using the exact arguments specified in `run.sh` after loading the newly minted conda environment.

We mapped each figure or table to the corresponding experiment directory below. The code used to plot figures is placed under the figures folder.


|    Figure/Table   |                                                                      Directory                                                                      |                                                                                Remark                                                                               |
|:-----------------:|:---------------------------------------------------------------------------------------------------------------------------------------------------:|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Figure 3.1        | al_baselines/cifar10/ensemble/bald10                                                                                                                | Run the code and extract BALD scores from metrics/ folder                                                                                                           |
| Figure 3.2a       | al_baselines/mnist/bald1, al_baselines/mnist/random10                                                                                               | -                                                                                                                                                                   |
| Figure 3.2b       | new_cifar/03b_random_imbalanced, new_cifar/03a_bald_imbalanced, al_baselines/cifar10/ensemble/random50                                              | -                                                                                                                                                                   |
| Figure 3.3a       | new_cifar/03a_bald_imbalanced                                                                                                                       | Run the code and extract data distribution from metrics/ folder,  then load model from saved_weights folder and run it on the CIFAR-10 test set to get the top row. |
| Figure 3.3b       | exploration/ensemble/cifar                                                                                                                          | -                                                                                                                                                                   |
| Figure 4.2a       | ephemeral/mnist                                                                                                                                     | BALD_SSL => Post-acquisition SSL_BALD => Pre-acquisition SSL_RA => random acquisition BALD-1 extracted from Figure 3.2a                                             |
| Figure 4.2b       | al_baselines/cifar10/ensemble/bald10, plmixup/cifar10/random100                                                                                     | -                                                                                                                                                                   |
| Table 4.1         | plmixup/cifar10/random100                                                                                                                           | Harvest from the same experiment                                                                                                                                    |
| Figure 4.3        | vanilla_repeated_acquisition/mnist/reconsider/more_iters_diff_dataset                                                                               |                                                                                                                                                                     |
| Figure 4.4a       | vanilla_repeated_acquisition/mnist/                                                                                                                 | permanent => Permanent reconsider => Ephemeral                                                                                                                      |
| Figure 4.4b       | vanilla_repeated_acquisition/mnist/                                                                                                                 | Extract data from pl_metrics/ folder                                                                                                                                |
| Table 4.2         | new_mnist/01_eph_imbalanced/balanced_fixed, new_mnist/01_eph_imbalanced/fixed, new_mnist/02f_random_plmixup_initial_imbalanced                      | balanced_fixed => Balanced algo 1 fixed => Imbalanced algo 1 02f => Imbalanced algo 2                                                                               |
| Table 4.3         | ephemeral/cifar10, plmixup/cifar10/random100                                                                                                        | ephemeral => algo 1 random100 => harvest from experiment                                                                                                            |
| Figure 4.5 (a, b) | ephemeral/cifar10, plmixup/cifar10/random100                                                                                                        | Run code and plot from calib_metrics/ folder                                                                                                                        |
| Figure 5.1a       | cifar10/noisy_tbald cifar10/kl_augmented                                                                                                            | noisy_tbald => TBALD kl_augmented => LADI                                                                                                                           |
| Figure 5.1b       | cifar10/noisy_tbald cifar10/kl_augmented                                                                                                            | Load models from saved_models folder and score unlabelled pool                                                                                                      |
| Figure 5.2        | cifar10/noisy_tbald cifar10/kl_augmented                                                                                                            | -                                                                                                                                                                   |
| Figure 5.3        | new_cifar/06a_pre_acq_bald new_cifar/05b_post_kl_imbalanced new_cifar/04c_kl new_cifar/04a_post_bald_imbalanced new_cifar/04b_random_imbalanced_ssl | 06a => Pre-acquisition BALD 05b => Pre-evaluation LADI 04c => Pre-acquisition LADI 04a => Pre-evaluation BALD 04b => Random acquisition                             |
| Table 5.1         | cifar10/kl_augmented                                                                                                                                | Run experiment and extract accuracy                                                                                                                                 |
| Figure 5.4a       | plmixup/cifar10/tryouts/explore                                                                                                                     | -                                                                                                                                                                   |
| Figure 5.4b       | plmixup/cifar10/tryouts/explore plmixup/cifar10/tryouts/without_jitter                                                                              | explore => Standard Aug (+ Colour Jitter) without_jitter => No Aug.                                                                                                 |
| Figure 5.5a       | al_baselines/cifar10/bald10, al_baselines/cifar10/random10                                                                                          | -                                                                                                                                                                   |
| Figure 5.5b       | al_baselines/cifar10/ensemble/bald10, al_baselines/cifar10/ensemble/random50                                                                        | -                                                                                                                                                                   |
| Figure 5.6 (a,b)  | exploration/ood/bald10_svhn_ood                                                                                                                     | Run code and plot manually.                                                                                                                                         |
| Figure 5.7 (a,b)  | exploration/consistent_presnet                                                                                                                      | Run code and plot manually.                                                                                                                                         |
| Figure A.1        | vanilla_repeated_acquisition/mnist/                                                                                                                 | Extract data from pl_metrics/ folder                                                                                                                                |
| Figure A.2        | exploration/ensemble/cifar                                                                                                                          | Extract from calib_metrics folder                                                                                                                                   |