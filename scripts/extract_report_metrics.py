#!/usr/bin/env python3
"""Extract additional report metrics from completed, decrypted lab captures."""
import argparse
import csv
import json
import statistics
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET


def packets(capture, keys, display_filter):
    command=['tshark','-n','-r',str(capture),'-o',f'tls.keylog_file:{keys}','-Y',display_filter,'-T','pdml']
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for event,packet in ET.iterparse(process.stdout,events=('end',)):
            if packet.tag=='packet':
                yield packet
                packet.clear()
        errors=process.stderr.read().decode()
        if process.wait():
            raise RuntimeError(errors)
    finally:
        process.stdout.close();process.stderr.close()
        if process.poll() is None:
            process.kill();process.wait()


def value(packet,name,default=None):
    field=packet.find(f'.//*[@name="{name}"]')
    return field.get('show',default) if field is not None else default


def ranges_overlap(ranges,start,end):
    return sum(max(0,min(end,b)-max(start,a)) for a,b in ranges)


def add_range(ranges,start,end):
    merged=[]
    for a,b in sorted(ranges+[(start,end)]):
        if merged and a<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:
            merged.append((a,b))
    return merged


def extract(suite, output):
    output.mkdir(parents=True,exist_ok=False)
    records=[];request_rows=[];handshakes=[];retransmissions=[];recovery=[]
    for file in sorted(suite.glob('*/result.json')):
        trial=file.parent;meta=json.loads(file.read_text())
        if meta['status']!='ok':
            continue
        base=dict(trial=trial.name,experiment=meta['experiment'],protocol=meta['protocol'],rtt_ms=meta['rtt_ms'],loss_pct=meta['loss_pct'],run=meta['run'])
        keys=suite/'server.keys'
        if meta['experiment']=='page':
            lines=[line.split() for line in (trial/'requests.tsv').read_text().splitlines() if line.strip()]
            durations=[float(row[2])/1e6 for row in lines if row[1]=='200']
            records.append(dict(base,metric='request_median_s',value=statistics.median(durations)))
            for i,row in enumerate(lines,1):
                request_rows.append(dict(base,log_row=i,start_epoch_us=row[0],http_status=row[1],duration_s=float(row[2])/1e6))
        if meta['experiment'].startswith('handshake'):
            responses=[];first_request=None
            display='(ip.src == 10.0.1.10 && (http2.type == 1 || http3.frame_type == 1)) || (ip.src == 10.0.2.10 && (tcp.flags.syn == 1 || tls.handshake.type == 2))'
            for packet in packets(trial/'r1.pcapng',keys,display):
                frame=int(value(packet,'frame.number'))
                if value(packet,'ip.src')=='10.0.1.10':
                    first_request=frame;break
                responses.append(frame)
            if first_request is None and meta['experiment'] == 'handshake_0rtt' and meta.get('early_data_bytes', 0) > 0:
                command = ['tshark', '-r', str(trial/'r1.pcapng'), '-Y',
                           'ip.src == 10.0.1.10 && quic.long.packet_type == 1 && quic.version == 0x00000001',
                           '-T', 'fields', '-e', 'frame.number']
                frames = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).split()
                if frames:
                    first_request = int(frames[0])
                    responses = [frame for frame in responses if frame < first_request]
            verified=first_request is not None
            handshakes.append(dict(base,first_request_frame=first_request,preceding_server_response_frames=' '.join(map(str,responses)),required_response_round_trips=len(responses) if verified else '',verification=('captured 0-RTT packet and client-verified accepted early data' if meta['experiment']=='handshake_0rtt' else 'request and prerequisite server response flights decoded') if verified else 'first request not decoded'))
            if verified:
                records.append(dict(base,metric='request_ready_round_trips',value=len(responses)))
        if meta['experiment']=='bulk':
            seen={};seen_packets=set();repeated_bytes=0;frames=set()
            for packet in packets(trial/'r2.pcapng',keys,'ip.src == 10.0.2.10 && (tcp.len > 0 || quic.stream.stream_id)'):
                frame=int(value(packet,'frame.number'));timestamp=float(value(packet,'frame.time_relative'))
                segments=[]
                if meta['protocol']=='h2':
                    segments=[('tcp',int(value(packet,'tcp.seq_raw')),int(value(packet,'tcp.len')))]
                    identity=('tcp',frame)
                else:
                    pn=value(packet,'quic.packet_number')
                    if pn is None:
                        continue
                    identity=('quic',pn)
                    if identity in seen_packets:
                        continue
                    for parent in packet.iter():
                        children={f.get('name'):f for f in parent if f.tag=='field'}
                        if 'quic.stream.stream_id' not in children:
                            continue
                        sid=children['quic.stream.stream_id'].get('show')
                        offset=int(children['quic.stream.offset'].get('show')) if 'quic.stream.offset' in children else 0
                        data=children.get('quic.stream_data')
                        length=int(children['quic.stream.length'].get('show')) if 'quic.stream.length' in children else int(data.get('size','0')) if data is not None else 0
                        segments.append((sid,offset,length))
                seen_packets.add(identity)
                for stream,start,length in segments:
                    if not length:continue
                    previous=seen.setdefault(stream,[])
                    overlap=ranges_overlap(previous,start,start+length)
                    if overlap:
                        retransmissions.append(dict(base,sender_frame=frame,time_s=timestamp,stream=stream,offset=start,length=length,retransmitted_transport_bytes=overlap,packet_identity=str(identity)))
                        repeated_bytes+=overlap;frames.add(frame)
                    seen[stream]=add_range(previous,start,start+length)
            records.extend([dict(base,metric='sender_retransmitted_transport_bytes',value=repeated_bytes),dict(base,metric='sender_frames_with_retransmitted_data',value=len(frames))])
            gapfile=trial/'trace-analysis/gaps.csv'
            if gapfile.exists():
                for gap in csv.DictReader(gapfile.open()):
                    recovery.append(dict(base,**gap))
        print('Analyzed',trial.name,flush=True)
    for name,rows in [('metrics',records),('request-durations',request_rows),('handshake-round-trips',handshakes),('retransmissions',retransmissions),('receiver-gap-recovery',recovery)]:
        if rows:
            with (output/f'{name}.csv').open('w') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (output/'definitions.json').write_text(json.dumps({
        'request_median_s':'Median successful request duration per trial from h2load log; log rows have no explicit stream ID.',
        'request_ready_round_trips':'Count of server SYN-ACK and ServerHello response flights seen before the first decoded client request. Required handshake response exchanges, not elapsed time divided by configured RTT.',
        'retransmissions':'Repeated sender-side TCP byte ranges or QUIC stream ranges sent in distinct packet numbers. TCP includes TLS overhead. QUIC includes HTTP/3 framing. Frames are capture frames, potentially containing multiple QUIC packets.',
        'recovery':'Receiver-side observed gap intervals; may include reordering. Loss-event attribution and application stall measurement require additional correlation.',
    },indent=2))


    import pandas as pd
    from scipy.stats import t
    table = pd.DataFrame(records)
    summary = []
    keys = ['experiment', 'protocol', 'rtt_ms', 'loss_pct', 'metric']
    for identity, group in table.groupby(keys):
        values = group.value
        n = len(values)
        mean = values.mean()
        sd = values.std(ddof=1)
        margin = t.ppf(.975, n-1)*sd/n**.5 if n > 1 else float('nan')
        summary.append(dict(zip(keys, identity), n=n, mean=mean, sd=sd,
                            ci95_low=mean-margin, ci95_high=mean+margin))
    pd.DataFrame(summary).to_csv(output/'summary.csv', index=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite',type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();extract(args.suite,args.output)
