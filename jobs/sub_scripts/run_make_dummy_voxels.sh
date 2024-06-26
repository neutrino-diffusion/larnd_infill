#!/bin/bash
#SBATCH -p RCIF
#SBATCH -N1
#SBATCH -c2
#SBATCH --mem=10000
#SBATCH --array=0-21
#SBATCH --error=/home/awilkins/larnd_infill/larnd_infill/jobs/logs/err/job%j.err
#SBATCH --output=/home/awilkins/larnd_infill/larnd_infill/jobs/logs/out/job%j.out

OUTPUT_DIR=$1
VMAP_PATH=$2
N=$3
MODE=$4

START_IDX=$((${N}*${SLURM_ARRAY_TASK_ID}))

echo "Job id ${SLURM_JOB_ID}"
echo "Job array task id ${SLURM_ARRAY_TASK_ID}"
echo "Running on ${SLURM_JOB_NODELIST}"
echo "output dir is ${OUTPUT_DIR}"
echo "voxel map is ${VMAP_PATH}"
echo "number to process is ${N}"
echo "starting index is ${START_IDX}"
echo "mode is ${MODE} (1: normal, 2: --fix_y --fix_z --num_packet_features)"

cd /home/awilkins/larnd_infill/larnd_infill
source setups/setup.sh

if [[ "$MODE" == 1 ]]; then
  python data_scripts/make_dummy_larnd_voxels.py --batch_mode \
                                                 --start_index $START_IDX \
                                                 $OUTPUT_DIR \
                                                 $VMAP_PATH \
                                                 $N
elif [[ "$MODE" == 2 ]]; then
  python data_scripts/make_dummy_larnd_voxels.py --batch_mode \
                                                 --start_index $START_IDX \
                                                 --fix_y \
                                                 --fix_z \
                                                 --num_packet_feature \
                                                 $OUTPUT_DIR \
                                                 $VMAP_PATH \
                                                 $N
else
  echo "invalid mode (${MODE})"
  exit 1
fi

