#!/usr/bin/env python3
"""Build transport-gap evidence from analyzed page trials."""
import argparse
import os
os.environ.setdefault('MPLCONFIGDIR', '/tmp/cnproject-matplotlib')
from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd


def build(folder):
    records=[]
    for trial in sorted(folder.glob('*-page-*')):
        analysis=trial/'trace-analysis'
        if not (analysis/'summary.json').exists():
            continue
        summary=json.loads((analysis/'summary.json').read_text())
        if summary['evidence_status']!='sufficient_transport_bytes':
            continue
        metadata=json.loads((trial/'result.json').read_text())
        progress=pd.read_csv(analysis/'stream-progress.csv')
        gaps=pd.read_csv(analysis/'gaps.csv')
        if metadata['protocol']=='h3':
            # Client-initiated bidirectional HTTP/3 request streams have ID 0 mod 4.
            progress=progress[progress['stream'].map(lambda s:int(s.rsplit('-',1)[1])%4==0)]
            gaps=gaps[gaps['stream'].isin(progress['stream'])]
        gap_records=[]
        for gap in gaps.to_dict('records'):
            others=[]
            for stream, data in progress.groupby('stream',sort=False):
                if stream==gap['stream']:
                    continue
                data=data.sort_values(['time_s','frame'])
                before=data[data.time_s<=gap['start_s']]
                until=data[data.time_s<gap['end_s']]
                initial=before.contiguous_end.iloc[-1] if len(before) else 0
                final=until.contiguous_end.iloc[-1] if len(until) else 0
                if final>initial:
                    others.append(stream)
            gap_records.append(dict(gap,other_request_streams_progressing=len(others),
                                    other_stream_ids=' '.join(others)))
        pd.DataFrame(gap_records,columns=list(gaps.columns)+['other_request_streams_progressing','other_stream_ids']).to_csv(analysis/'gap-evidence.csv',index=False)
        figure,axes=plt.subplots(2,1,figsize=(10,7),layout='constrained')
        for stream,data in progress.groupby('stream',sort=False):
            data=data.sort_values(['time_s','frame'])
            axes[0].step(data.time_s,data.contiguous_end/1024,where='post',alpha=.45)
        axes[0].set(xlabel='Time from capture start (s)',ylabel='Contiguous transport data (KiB)',
                    title=f'{metadata["protocol"].upper()} • {metadata["loss_pct"]}% configured loss per direction')
        if gap_records:
            selected=max(gap_records,key=lambda g:g['duration_s'])
            start,end=selected['start_s'],selected['end_s']
            chosen=[selected['stream']]+selected['other_stream_ids'].split()[:5]
            padding=max((end-start)*.25,.01)
            for stream in chosen:
                data=progress[progress.stream==stream].sort_values(['time_s','frame'])
                before=data[data.time_s<=start]
                initial=before.contiguous_end.iloc[-1] if len(before) else 0
                window=data[(data.time_s>=start-padding)&(data.time_s<=end+padding)]
                axes[1].step(window.time_s-start,(window.contiguous_end-initial)/1024,where='post',label=stream)
            axes[1].axvspan(0,end-start,color='orange',alpha=.2)
            axes[1].set_title(f'Longest observed gap • frames {selected["first_frame"]}–{selected["recovery_frame"]}; '
                              f'{selected["other_request_streams_progressing"]} other request streams progressed')
            axes[1].legend(fontsize=8)
            axes[1].set(xlabel='Time relative to first observed gap (s)',ylabel='Change in contiguous data (KiB)')
        else:
            axes[1].text(.5,.5,'No completed request-stream gaps observed',ha='center',va='center',transform=axes[1].transAxes)
            axes[1].set_axis_off()
        figure.savefig(analysis/'hol-evidence.png',dpi=180)
        plt.close(figure)
        records.append(dict(trial=trial.name,protocol=metadata['protocol'],loss_pct=metadata['loss_pct'],
                            completed_transport_gaps=len(gap_records),
                            max_gap_s=max((g['duration_s'] for g in gap_records),default=0),
                            gaps_with_other_request_progress=sum(g['other_request_streams_progressing']>0 for g in gap_records),
                            note='TCP is one transport byte stream. QUIC rows count request-stream gaps. Router arrivals and reordering may create gaps; application delivery is not measured.'))
    pd.DataFrame(records).to_csv(folder/'hol-evidence.csv',index=False)
    print(f'Generated evidence for {len(records)} page trials in {folder}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results',type=Path)
    build(parser.parse_args().results)
