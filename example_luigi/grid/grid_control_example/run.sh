#!/bin/bash
export OMP_NUM_THREADS=1
export NUMEXPR_MAX_THREADS=1

mkdir -p out
mkdir -p figures

python create_graph.py
luigi --module netsalt.tasks PlotControllability --local-scheduler --log-level INFO

