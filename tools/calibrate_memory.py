#!/usr/bin/env python3
"""Record a short baseline; no model loads. Re-run after workload/hardware changes."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import stability

if __name__ == '__main__':
    samples = []
    for _ in range(20):
        samples.append(stability.memory_sample())
        time.sleep(1)
    peak = max(s['baseline_bytes'] for s in samples)
    policy = {'version': 1, 'created': time.time(), 'mem_total_bytes': samples[-1]['total'],
              'min_available_bytes': int(peak * 1.25), 'max_full_psi_avg10': 10,
              'measurement': {'samples': len(samples), 'baseline_peak_bytes': peak,
                              'method': 'AnonPages+Shmem+SUnreclaim+PageTables+KernelStack'},
              'policy_note': 'Reserve is 1.25x observed baseline; PSI full avg10 >=10% refuses. Policy margins, not CUDA capacity predictions.'}
    stability.DATA.mkdir(exist_ok=True)
    tmp = stability.POLICY.with_suffix('.tmp')
    tmp.write_text(json.dumps(policy, indent=2))
    tmp.replace(stability.POLICY)
    print(json.dumps(policy, indent=2))
