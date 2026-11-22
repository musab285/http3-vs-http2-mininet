#!/usr/bin/env python3
"""Assemble verified workload and handshake measurements for the report."""
import argparse
import json
import os
import tempfile
from pathlib import Path
os.environ.setdefault('MPLCONFIGDIR',tempfile.mkdtemp(prefix='cn-mpl-'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from scipy.stats import t


def finalize(workloads, handshakes, output, pages=None):
    rows=[]
    for source, experiments in [(workloads, {'bulk'} if pages else {'page','bulk'}),
                                *([(pages, {'page'})] if pages else []),
                                (handshakes, {'handshake','handshake_resumed','handshake_0rtt'})]:
        for path in sorted(source.glob('*/result.json')):
            row=json.loads(path.read_text())
            if row['experiment'] not in experiments:
                continue
            if row['status']!='ok' or row.get('trace_evidence_status')!='sufficient_transport_bytes':
                raise ValueError(f'Unverified trial: {path}')
            if row['experiment'] == 'page' and 'request_median_s' not in row:
                import statistics
                durations = [float(line.split()[2])/1e6 for line in (path.parent/'requests.tsv').read_text().splitlines() if line.strip()]
                row['request_median_s'] = statistics.median(durations)
            row['source_trial']=str(path.parent.resolve())
            rows.append(row)
    data=pd.DataFrame(rows)
    keys=['experiment','rtt_ms','loss_pct','rate_mbit','protocol']
    if data.empty:
        raise ValueError('No verified trials')
    if data.duplicated(keys+['run']).any():
        raise ValueError('Duplicate trial identity')
    if (data.groupby(keys).size()<3).any():
        raise ValueError('At least three successful repetitions required per group')
    output.mkdir(parents=True,exist_ok=False)
    data.to_csv(output/'trials.csv',index=False)
    metrics={'page':['page_s','request_median_s','request_p95_s'], 'bulk':['total_s','goodput_mbit']}
    result=[]
    for identity, group in data.groupby(keys):
        experiment=identity[0]
        selected_metrics = list(metrics.get(experiment,['setup_s','ttfb_s']))
        if experiment == 'page':
            selected_metrics += [metric for metric in ('requests_with_correlated_delivery_pause', 'correlated_delivery_pause_request_seconds', 'correlated_delivery_pause_wall_seconds') if metric in group and group[metric].notna().all()]
        for metric in selected_metrics:
            values=group[metric].dropna()
            n=len(values)
            if n!=len(group):
                raise ValueError(f'Missing {metric} in {identity}')
            mean=values.mean();sd=values.std(ddof=1)
            margin=t.ppf(.975,n-1)*sd/(n**.5)
            result.append(dict(zip(keys,identity),metric=metric,n=n,mean=mean,sd=sd,
                               ci95_low=mean-margin,ci95_high=mean+margin,
                               minimum=values.min(),maximum=values.max()))
    summary=pd.DataFrame(result)
    summary.to_csv(output/'summary.csv',index=False)
    labels={'request_median_s':'Within-trial median request duration (s)', 'setup_s':'Connection-ready time (s)','ttfb_s':'Time to first byte (s)',
            'page_s':'Page-load time (s)','request_p95_s':'Within-trial 95th-percentile request duration (s)',
            'requests_with_correlated_delivery_pause':'Requests with gap-correlated delivery pauses',
            'correlated_delivery_pause_request_seconds':'Sum of gap-correlated pause time across requests (s)',
            'correlated_delivery_pause_wall_seconds':'Time with any gap-correlated delivery pause (s)',
            'total_s':'Transfer time (s)','goodput_mbit':'Whole-transfer goodput (Mbit/s)'}
    for (experiment,metric), group in summary.groupby(['experiment','metric']):
        x='rtt_ms' if experiment.startswith('handshake') else 'loss_pct'
        fig,ax=plt.subplots(figsize=(7,4.5),layout='constrained')
        for protocol, series in group.groupby('protocol'):
            series=series.sort_values(x)
            errors=[series['mean']-series.ci95_low,series.ci95_high-series['mean']]
            ax.errorbar(series[x],series['mean'],yerr=errors,marker='o',capsize=4,label=protocol.upper())
        ax.set_xlabel('Configured RTT (ms)' if x=='rtt_ms' else 'Configured packet loss per direction (%)')
        ax.set_ylabel(labels[metric]);ax.set_title(experiment.replace('_',' ')+' • mean and 95% CI')
        ax.legend();fig.savefig(output/f'{experiment}-{metric}.png',dpi=180);plt.close(fig)
    manifest={'workload_source':str(workloads.resolve()),'handshake_source':str(handshakes.resolve()), 'page_source':str((pages or workloads).resolve()),
              'verified_trials':len(data),'conditions':len(data.groupby(keys)),
              'page_measurement':'Instrumented libcurl client; monotonic body-delivery timestamps; one connection; request durations start at outgoing request headers.',
              'method':'Sample standard deviation; two-sided Student t confidence interval for each mean. Cold handshakes sourced only from dedicated handshake sweep.',
              'limitations':['Three repetitions per group meet the common minimum; proposal requests 30.',
                             'Confidence intervals describe sampling variation across these trials.',
                             'Application callback pauses are correlated with observed transport gaps; correlation alone does not establish packet-loss causality.']}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workloads',required=True,type=Path)
    parser.add_argument('--handshakes',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--pages',type=Path,help='Instrumented page sweep replacing the original page results')
    args=parser.parse_args()
    finalize(args.workloads,args.handshakes,args.output,args.pages)
