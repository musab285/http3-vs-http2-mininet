#!/usr/bin/env python3
"""Plot the runner's summary.csv with standard-deviation error bars."""
import os
os.environ.setdefault('MPLCONFIGDIR', '/tmp/cnproject-matplotlib')
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

folder = Path(sys.argv[1])
data = pd.read_csv(folder / 'summary.csv')
for (experiment, metric), group in data.groupby(['experiment', 'metric']):
    x = 'rtt_ms' if experiment.startswith('handshake') else 'loss_pct'
    fig, ax = plt.subplots()
    for protocol, rows in group.groupby('protocol'):
        rows = rows.sort_values(x)
        ax.errorbar(rows[x], rows['mean'], yerr=rows['sd'].fillna(0), marker='o', capsize=4, label=protocol)
    ax.set_xlabel('Configured RTT (ms)' if x == 'rtt_ms' else 'Configured loss per direction (%)')
    ax.set_ylabel(metric)
    ax.set_title(experiment)
    ax.legend()
    fig.tight_layout()
    fig.savefig(folder / f'{experiment}-{metric}.png', dpi=160)
    plt.close(fig)
