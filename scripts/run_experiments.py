#!/usr/bin/env python3
"""Run routed HTTP/2 and HTTP/3 experiments and retain every trial."""
import argparse
import csv
import json
import os
from pathlib import Path
import random
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def execute(node, args, timeout=180, log_dir=None):
    process = node.popen([str(arg) for arg in args], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    started = time.monotonic()
    expired = False
    try:
        while True:
            remaining = timeout - (time.monotonic() - started)
            try:
                out, err = process.communicate(timeout=max(.01, min(15, remaining)))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= timeout:
                    expired = True
                    process.kill()
                    out, err = process.communicate()
                    break
                if log_dir is not None:
                    print(f'  {log_dir.name}: running for {int(time.monotonic()-started)}s (limit {timeout}s)', flush=True)
    except BaseException:
        process.kill()
        out, err = process.communicate()
        if log_dir is not None:
            (log_dir / 'client.stdout').write_bytes(out)
            (log_dir / 'client.stderr').write_bytes(err)
        raise
    if log_dir is not None:
        (log_dir / 'client.stdout').write_bytes(out)
        (log_dir / 'client.stderr').write_bytes(err)
        (log_dir / 'process.json').write_text(json.dumps({
            'command': [str(arg) for arg in args], 'exit_code': process.returncode,
            'watchdog_timeout': expired, 'elapsed_s': time.monotonic()-started,
        }, indent=2))
    if expired:
        raise RuntimeError(f'Process exceeded {timeout}s; partial output saved in {log_dir}')
    if process.returncode:
        raise RuntimeError(f'Exit {process.returncode}: {err.decode(errors="replace")}')
    return out.decode(), err.decode()


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def summarize(destination):
    records = [json.loads(path.read_text()) for path in destination.glob('*/result.json')]
    fields = sorted(set().union(*(record.keys() for record in records))) if records else []
    with (destination / 'trials.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    groups = {}
    for record in records:
        key = (record['experiment'], record['rtt_ms'], record['loss_pct'], record['rate_mbit'], record['protocol'])
        groups.setdefault(key, []).append(record)
    summaries = []
    for key, trials in groups.items():
        for metric in ('total_s', 'ttfb_s', 'setup_s', 'goodput_mbit', 'page_s', 'request_median_s', 'request_p95_s'):
            values = [row[metric] for row in trials if row['status'] == 'ok' and metric in row]
            if values:
                summaries.append(dict(zip(('experiment', 'rtt_ms', 'loss_pct', 'rate_mbit', 'protocol'), key),
                                      metric=metric, runs=len(values), failed=sum(r['status'] != 'ok' for r in trials),
                                      mean=statistics.mean(values), sd=statistics.stdev(values) if len(values)>1 else '',
                                      minimum=min(values), maximum=max(values)))
    if summaries:
        with (destination / 'summary.csv').open('w') as handle:
            writer = csv.DictWriter(handle, fieldnames=summaries[0].keys())
            writer.writeheader()
            writer.writerows(summaries)
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caddy', type=Path, default=ROOT / '.tools/bin/caddy',
                        help='Caddy executable for both protocols')
    parser.add_argument('--config', type=Path, default=ROOT / 'experiments.json')
    parser.add_argument('--runs', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--experiment', help='Run only this experiment, for example page')
    parser.add_argument('--bulk-timeout', type=int, default=1200,
                        help='Bulk transfer limit in seconds (default 1200)')
    parser.add_argument('--retry-failed', type=Path,
                        help='Rerun failed trials from this results directory into a new directory')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    runs = config['runs'] if args.runs is None else args.runs
    if args.bulk_timeout < 1:
        parser.error('bulk timeout must be positive')
    if runs < 1:
        parser.error('runs must be positive')
    jobs = [(profile, run, protocol) for profile in config['profiles'] for run in range(1, runs+1) for protocol in profile.get('protocols', ('h2','h3'))]
    if args.retry_failed:
        records = sorted(args.retry_failed.glob('*/result.json'))
        if not records:
            parser.error('retry directory contains no trial results')
        jobs = []
        for path in records:
            record = json.loads(path.read_text())
            if record['status'] != 'ok':
                profile = {key: record[key] for key in ('experiment', 'rtt_ms', 'loss_pct', 'rate_mbit')}
                jobs.append((profile, record['run'], record['protocol']))
        if not jobs:
            print('No failed trials to retry.')
            return
    if args.experiment:
        jobs = [job for job in jobs if job[0]['experiment'] == args.experiment]
        if not jobs:
            parser.error('No trials match the selected experiment')
    random.Random(config['seed']).shuffle(jobs)
    if args.dry_run:
        print(json.dumps({'trials':len(jobs), 'runs':runs, 'profiles':config['profiles']}, indent=2))
        return
    if os.geteuid() != 0:
        parser.error('run with sudo /usr/bin/python3')
    from topology import create_network
    destination = ROOT / 'results' / datetime.now(timezone.utc).strftime('automated-%Y%m%dT%H%M%S-%fZ')
    destination.mkdir()
    (destination / 'configuration.json').write_text(json.dumps(dict(config, runs=runs, bulk_timeout_s=args.bulk_timeout, retry_source=str(args.retry_failed) if args.retry_failed else None, scheduled_trials=[dict(profile, run=run, protocol=protocol) for profile, run, protocol in jobs]), indent=2))
    curl = ROOT / '.tools/bin/curl'
    h2load = ROOT / '.tools/bin/h2load'
    keys = destination / 'server.keys'
    data = destination / 'caddy-data'
    caddyfile = destination / 'Caddyfile'
    caddyfile.write_text(f'''{{
 admin off
 skip_install_trust
 default_sni 10.0.2.10
 servers {{
  protocols h1 h2 h3
 }}
}}
https://10.0.2.10 {{
 tls internal {{
  protocols tls1.3
  insecure_secrets_log {keys}
 }}
 root * {ROOT / 'content'}
 header Cache-Control no-store
 file_server
}}
''')
    ca = data / 'caddy/pki/authorities/local/root.crt'
    net = None
    server = None
    server_log = (destination / 'server.log').open('w')
    try:
        net = create_network()
        client = net['h1']
        # Virtual-interface offloads can merge UDP datagrams in captures and
        # make netem apply packet loss to aggregates. Disable them consistently.
        offload_logs = []
        for node in net.hosts:
            for interface in node.intfNames():
                if interface == 'lo':
                    continue
                execute(node, ['ethtool', '-K', interface, 'gro', 'off',
                               'gso', 'off', 'tso', 'off'])
                try:
                    execute(node, ['ethtool', '-K', interface,
                                   'tx-udp-segmentation', 'off'])
                except RuntimeError as error:
                    offload_logs.append(f'{node.name}/{interface}: {error}')
                features, _ = execute(node, ['ethtool', '-k', interface])
                offload_logs.append(f'{node.name}/{interface}\n{features}')
        (destination / 'interface-offloads.txt').write_text('\n'.join(offload_logs))
        server = net['h2'].popen([str(args.caddy),'run','--config',str(caddyfile)], cwd=str(ROOT),
                                env=dict(os.environ, XDG_DATA_HOME=str(data)), stdout=server_log, stderr=server_log)
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError('Caddy failed; inspect server.log')
            if ca.exists():
                break
            time.sleep(.1)
        base = [curl,'--noproxy','*','--cacert',ca,'-fsS','--max-time','180','-o','/dev/null','-w','%{json}']
        for protocol in ('h2', 'h3'):
            command = base + [
                '--http2' if protocol == 'h2' else '--http3-only',
                '--connect-timeout', '2', '--max-time', '3',
                'https://10.0.2.10/small.bin',
            ]
            attempts = []
            for attempt in range(10):
                if server.poll() is not None:
                    raise RuntimeError('Caddy exited during startup; inspect server.log')
                try:
                    out, err = execute(client, command, timeout=5)
                    metrics = json.loads(out)
                    expected = '2' if protocol == 'h2' else '3'
                    if str(metrics['http_version']) != expected:
                        raise RuntimeError(f'Expected HTTP/{expected}, got {metrics["http_version"]}')
                    if int(metrics['size_download']) != (ROOT / 'content/small.bin').stat().st_size:
                        raise RuntimeError('Startup response has an unexpected byte count')
                    (destination / f'preflight-{protocol}.json').write_text(out)
                    break
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    attempts.append(str(error))
                    (destination / f'preflight-{protocol}.log').write_text('\n'.join(attempts))
                    time.sleep(.5)
            else:
                raise RuntimeError(f'{protocol} startup check failed; inspect preflight-{protocol}.log and server.log')
        (destination / 'baseline-ping.txt').write_text(execute(client,['ping','-c','5','10.0.2.10'])[0])
        (destination / 'versions.txt').write_text(subprocess.check_output([str(curl),'-V'], text=True) + '\nCaddy: ' + subprocess.check_output([str(args.caddy), 'version'], text=True))
        for number, (profile, run, protocol) in enumerate(jobs, 1):
            trial = destination / f'{number:04d}-{profile["experiment"]}-{protocol}-run{run}'
            trial.mkdir()
            result = dict(profile, protocol=protocol, run=run, status='failed')
            print(f'{number}/{len(jobs)} START {profile["experiment"]} {protocol}, RTT={profile["rtt_ms"]}ms, loss={profile["loss_pct"]}%', flush=True)
            captures = []
            logs = []
            try:
                session_options = []
                if profile['experiment'] in ('handshake_resumed', 'handshake_0rtt'):
                    session_file = trial / 'session.txt'
                    prime = trial / 'priming'
                    prime.mkdir()
                    session_options = ['--ssl-sessions', str(session_file), '--verbose']
                    if profile['experiment'] == 'handshake_0rtt':
                        session_options.append('--tls-earlydata')
                    execute(client, base + ['--http2' if protocol == 'h2' else '--http3-only']
                            + session_options + ['https://10.0.2.10/small.bin'], timeout=190, log_dir=prime)
                    if not session_file.exists() or not session_file.stat().st_size:
                        raise RuntimeError('Client did not save a TLS session ticket')
                for router, interface in ((net['r1'],'r1-eth1'), (net['r2'],'r2-eth0')):
                    execute(router,['tc','qdisc','replace','dev',interface,'root','handle','1:','tbf','rate',f'{profile["rate_mbit"]}mbit','burst','64kb','latency','100ms'])
                    execute(router,['tc','qdisc','replace','dev',interface,'parent','1:1','handle','10:','netem','delay',f'{profile["rtt_ms"]/2}ms','loss',f'{profile["loss_pct"]}%','limit','10000'])
                    (trial / f'{router.name}-qdisc.txt').write_text(execute(router,['tc','-s','qdisc','show','dev',interface])[0])
                    log = (trial / f'{router.name}-capture.log').open('w')
                    logs.append(log)
                    capture = router.popen(['tcpdump','--immediate-mode','-Z','root','-U','-i',interface,'-s','0','-w',str(trial/f'{router.name}.pcap'),'port','443'], stdout=log, stderr=log)
                    captures.append(capture)
                time.sleep(.4)
                if any(p.poll() is not None for p in captures):
                    raise RuntimeError('Capture failed to start')
                if profile['experiment']=='page':
                    command = [ROOT / '.tools/bin/page_client', protocol, ca,
                               ROOT / 'urls.txt', trial, ROOT / 'content', '600']
                    out, err = execute(client, command, timeout=610, log_dir=trial)
                    page_metrics = json.loads(out)
                    result.update(page_metrics)
                    result['page_client'] = 'libcurl_body_callback'
                else:
                    filename = 'bulk.bin' if profile['experiment']=='bulk' else 'small.bin'
                    limit = args.bulk_timeout if profile['experiment'] == 'bulk' else 180
                    result['timeout_s'] = limit
                    command = base + ['--http2' if protocol == 'h2' else '--http3-only',
                                      '--max-time', str(limit), f'https://10.0.2.10/{filename}']
                    out, err = execute(client, command + session_options, timeout=limit+10, log_dir=trial)
                    metrics = json.loads(out)
                    if str(metrics['http_version']) != ('2' if protocol=='h2' else '3') or int(metrics['size_download']) != (ROOT/'content'/filename).stat().st_size:
                        raise RuntimeError('Protocol or byte-count mismatch')
                    if profile['experiment'] == 'handshake_0rtt':
                        result['early_data_bytes'] = metrics.get('tls_earlydata', 0)
                        if result['early_data_bytes'] <= 0:
                            raise RuntimeError('0-RTT was not used or was rejected; inspect client.stdout')
                    result.update(total_s=metrics['time_total'],ttfb_s=metrics['time_starttransfer'],setup_s=metrics['time_appconnect'],
                                  goodput_mbit=metrics['size_download']*8/metrics['time_total']/1e6)
                (trial/'client.stdout').write_text(out)
                (trial/'client.stderr').write_text(err)
                result['status']='ok'
            except Exception as error:
                result['error']=str(error)
            finally:
                time.sleep(.2)
                for capture in captures:
                    stop(capture)
                for log in logs:
                    log.close()
                for capture_file in trial.glob('*.pcap'):
                    subprocess.run(['editcap','-F','pcapng',str(capture_file),str(capture_file.with_suffix('.pcapng'))],check=True)
                if result['status'] == 'ok' and profile['experiment'] in ('handshake_resumed', 'handshake_0rtt'):
                    try:
                        evidence = subprocess.check_output([
                            'tshark', '-r', str(trial / 'r2.pcapng'),
                            '-o', f'tls.keylog_file:{keys}',
                            '-Y', 'tls.handshake.extensions.psk.identity.selected',
                            '-T', 'fields', '-e', 'frame.number',
                        ], text=True, stderr=subprocess.PIPE, timeout=60)
                        (trial / 'resumption-evidence.txt').write_text(evidence)
                        result['resumption_verified'] = bool(evidence.strip())
                        if not result['resumption_verified']:
                            raise RuntimeError('No server-selected PSK found in measured capture')
                    except Exception as error:
                        result['status'] = 'failed'
                        result['error'] = str(error)
                try:
                    expected_bytes = sum((ROOT / 'content/objects' / Path(url).name).stat().st_size
                                         for url in (ROOT / 'urls.txt').read_text().splitlines() if url.strip()) if profile['experiment'] == 'page' else (ROOT / 'content' / ('bulk.bin' if profile['experiment'] == 'bulk' else 'small.bin')).stat().st_size
                    subprocess.run([sys.executable, str(ROOT / 'scripts/analyze_traces.py'),
                                    str(trial / 'r1.pcapng'), '--keys', str(keys),
                                    '--output', str(trial / 'trace-analysis'),
                                    '--expected-bytes', str(expected_bytes)],
                                   check=True, stdout=subprocess.DEVNULL, timeout=180)
                    evidence = json.loads((trial / 'trace-analysis/summary.json').read_text())
                    result['trace_evidence_status'] = evidence['evidence_status']
                    if profile['experiment'] == 'page' and (trial / 'app-events.csv').exists():
                        subprocess.run([str(ROOT / '.venv/bin/python'),
                                        str(ROOT / 'scripts/analyze_delivery.py'), str(trial)],
                                       check=True, stdout=subprocess.DEVNULL, timeout=120)
                        delivery = json.loads((trial / 'trace-analysis/delivery-summary.json').read_text())
                        for metric in ('requests_with_correlated_delivery_pause', 'correlated_delivery_pause_request_seconds', 'correlated_delivery_pause_wall_seconds'):
                            result[metric] = delivery[metric]

                except Exception as error:
                    result['trace_analysis_error'] = str(error)
                (trial/'result.json').write_text(json.dumps(result,indent=2))
            print(f'{number}/{len(jobs)} {profile["experiment"]} {protocol}: {result["status"]}',flush=True)
    finally:
        if server:
            stop(server)
        server_log.close()
        if net:
            net.stop()
        summaries = summarize(destination)
        if summaries:
            subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/plot_results.py'), str(destination)], check=True)
        uid, gid = int(os.environ.get('SUDO_UID',0)), int(os.environ.get('SUDO_GID',0))
        for path in [destination,*destination.rglob('*')]:
            os.chown(path,uid,gid)
        print(f'Results: {destination}',flush=True)


if __name__ == '__main__':
    main()
