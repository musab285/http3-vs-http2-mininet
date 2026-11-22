#!/usr/bin/env python3
"""Correlate measured application delivery pauses with receiver transport gaps."""
import json
import subprocess
from pathlib import Path
import sys
import pandas as pd

trial=Path(sys.argv[1])
events=pd.read_csv(trial/'app-events.csv')
gaps=pd.read_csv(trial/'trace-analysis/gaps.csv')
first_epoch=float(subprocess.check_output(['tshark','-r',str(trial/'r1.pcapng'),'-c','1','-T','fields','-e','frame.time_epoch'],text=True,stderr=subprocess.DEVNULL).strip())
anchor=events.epoch_ns.iloc[0]/1e9-events.time_s.iloc[0]
rows=[]
for gap_index,gap in gaps.iterrows():
 start=first_epoch+gap.start_s-anchor;end=first_epoch+gap.end_s-anchor
 for request,group in events.groupby('request_id'):
  chunks=group[group.event=='data'].sort_values('time_s')
  before=chunks[chunks.time_s<=start];after=chunks[chunks.time_s>=end]
  during=chunks[(chunks.time_s>start)&(chunks.time_s<end)]
  # Require actual content delivery before and after the gap to define a pause.
  if len(before) and len(after) and during.empty:
   rows.append(dict(gap_index=gap_index,transport_stream=gap.stream,request_id=request,
                    gap_start_s=start,gap_end_s=end,last_delivery_s=before.time_s.iloc[-1],
                    next_delivery_s=after.time_s.iloc[0],gap_overlap_s=end-start,
                    delivery_pause_s=after.time_s.iloc[0]-before.time_s.iloc[-1],
                    first_frame=gap.first_frame,recovery_frame=gap.recovery_frame))
pd.DataFrame(rows,columns=['gap_index','transport_stream','request_id','gap_start_s','gap_end_s','last_delivery_s','next_delivery_s','gap_overlap_s','delivery_pause_s','first_frame','recovery_frame']).to_csv(trial/'trace-analysis/gap-delivery.csv',index=False)
def union_seconds(intervals):
 merged=[]
 for start,end in sorted(intervals):
  if merged and start<=merged[-1][1]:
   merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
  else:
   merged.append((start,end))
 return sum(end-start for start,end in merged)

per_request={}
for row in rows:
 per_request.setdefault(row['request_id'],[]).append((row['gap_start_s'],row['next_delivery_s']))
summary=dict(requests_with_correlated_delivery_pause=len(per_request),
             gap_request_pairs=len(rows),
             correlated_delivery_pause_request_seconds=sum(union_seconds(intervals) for intervals in per_request.values()),
             correlated_delivery_pause_wall_seconds=union_seconds([interval for intervals in per_request.values() for interval in intervals]),
             definition='A request delivered body bytes before and after the complete observed transport-gap interval, with no body-delivery callbacks during that interval. Counted pause time runs from the observed gap start to the next body delivery; overlapping intervals are merged. Correlation does not alone establish loss causality; receiver gaps can include reordering.')
(trial/'trace-analysis/delivery-summary.json').write_text(json.dumps(summary,indent=2))
import os,tempfile
os.environ.setdefault('MPLCONFIGDIR',tempfile.mkdtemp(prefix='cn-delivery-mpl-'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,ax=plt.subplots(figsize=(10,5),layout='constrained')
for request,group in events[events.event=='data'].groupby('request_id'):
 ax.step(group.time_s,group.total_bytes/1024,where='post',alpha=.5)
ax.set(xlabel='Time since client start (s)',ylabel='Body bytes delivered per request (KiB)',title='Measured application body delivery')
if len(gaps):
 gap=gaps.loc[gaps.duration_s.idxmax()]
 ax.axvspan(first_epoch+gap.start_s-anchor,first_epoch+gap.end_s-anchor,color='orange',alpha=.2,label='Longest observed transport gap')
 ax.legend()
fig.savefig(trial/'trace-analysis/application-delivery.png',dpi=180)
plt.close(fig)
print(json.dumps(summary))
