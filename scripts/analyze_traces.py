#!/usr/bin/env python3
"""Extract receiver-side transport gaps and duplicate data from a lab capture."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


class Stream:
    def __init__(self, start):
        self.end = start
        self.pending = []
        self.ranges = []
        self.gap = None
        self.duplicate_bytes = 0
        self.unique_bytes = 0

    def receive(self, start, length, timestamp, frame):
        end = start + length
        overlap = sum(max(0, min(end, right)-max(start, left)) for left, right in self.ranges)
        self.duplicate_bytes += overlap
        self.unique_bytes += length-overlap
        ranges = sorted(self.ranges + [(start, end)])
        merged = []
        for left, right in ranges:
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        self.ranges = merged
        if start > self.end and self.gap is None:
            self.gap = (timestamp, frame)
        for left, right in merged:
            if left <= self.end:
                self.end = max(self.end, right)
        still_missing = any(left > self.end for left, right in merged)
        if self.gap is not None and not still_missing:
            began, first_frame = self.gap
            self.gap = None
            return dict(start_s=began, end_s=timestamp, duration_s=timestamp-began,
                        first_frame=first_frame, recovery_frame=frame)
        return None


def analyze(capture, keys, output, expected_bytes=0):
    output.mkdir(parents=True, exist_ok=True)
    command = ['tshark', '-n', '-r', str(capture), '-o', f'tls.keylog_file:{keys}',
               '-Y', 'ip.src == 10.0.2.10 && (tcp.len > 0 || quic.stream.stream_id)', '-T', 'pdml']
    streams = {}
    count = 0
    gap_count = 0
    with (output/'tshark.log').open('w') as errors, (output/'gaps.csv').open('w') as gaps, (output/'stream-progress.csv').open('w') as progress:
        writer = csv.DictWriter(gaps, fieldnames=['stream','start_s','end_s','duration_s','first_frame','recovery_frame'])
        writer.writeheader()
        events = csv.writer(progress)
        events.writerow(['frame','time_s','stream','offset','length','contiguous_end','duplicate_bytes_total'])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            for event, packet in ET.iterparse(process.stdout, events=('end',)):
                if packet.tag != 'packet':
                    continue
                fields = {f.get('name'): f.get('show') for f in packet.iter('field')}
                timestamp = float(fields['frame.time_relative'])
                frame = int(fields['frame.number'])
                segments = []
                if 'tcp.len' in fields:
                    name = 'tcp-'+fields['tcp.stream']
                    segments.append((name,int(fields['tcp.seq']),int(fields['tcp.len']),1))
                for parent in packet.iter():
                    children = {f.get('name'):f for f in parent if f.tag=='field'}
                    if 'quic.stream.stream_id' not in children:
                        continue
                    sid = children['quic.stream.stream_id'].get('show')
                    # Include connection identity so future captures can contain multiple connections.
                    name = 'quic-'+fields.get('udp.stream','0')+'-'+sid
                    offset = int(children['quic.stream.offset'].get('show')) if 'quic.stream.offset' in children else 0
                    data = children.get('quic.stream_data')
                    length = int(children['quic.stream.length'].get('show')) if 'quic.stream.length' in children else int(data.get('size','0')) if data is not None else 0
                    segments.append((name,offset,length,0))
                for name, offset, length, initial in segments:
                    if length <= 0:
                        continue
                    stream = streams.setdefault(name,Stream(initial))
                    gap = stream.receive(offset,length,timestamp,frame)
                    if gap:
                        writer.writerow(dict(stream=name,**gap))
                        gap_count += 1
                    events.writerow([frame,timestamp,name,offset,length,stream.end,stream.duplicate_bytes])
                    count += 1
                packet.clear()
            if process.wait() != 0:
                raise RuntimeError(f'tshark failed; inspect {output}/tshark.log')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    summary = dict(capture=str(capture.resolve()),segments=count,completed_gap_intervals=gap_count,
                   duplicate_transport_bytes=sum(s.duplicate_bytes for s in streams.values()),
                   unique_transport_bytes=sum(s.unique_bytes for s in streams.values()),
                   unresolved_stream_gaps=sum(s.gap is not None for s in streams.values()),
                   streams=len(streams),
                   interpretation='Arrival-order transport gaps at the R1 core interface. Duplicate ranges can reflect retransmission or duplication. These metrics do not measure application delivery stalls. TCP includes TLS overhead; QUIC includes HTTP/3 stream framing. Capture completeness and decryption must be checked.')
    summary['expected_content_bytes'] = expected_bytes
    summary['evidence_status'] = 'sufficient_transport_bytes' if summary['unique_transport_bytes'] >= expected_bytes and count else 'insufficient_capture_or_decryption'
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture',type=Path)
    parser.add_argument('--keys',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--expected-bytes', type=int, default=0)
    args=parser.parse_args()
    print(json.dumps(analyze(args.capture,args.keys,args.output,args.expected_bytes),indent=2))
