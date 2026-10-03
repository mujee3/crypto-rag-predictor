import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from run import load_instruments,scan_batch,main

class MultiCoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name)/'coins.json'

    def write(self,value):
        self.file.write_text(json.dumps(value));return str(self.file)

    def test_normalization_dedup_and_both_formats(self):
        p=self.write({'coins':['btc','BTCUSDT','Bitcoin','ETH','SEI']})
        self.assertEqual(load_instruments(coins_file=p),['BTC-USDT-SWAP','ETH-USDT-SWAP','SEI-USDT-SWAP'])
        self.assertEqual(load_instruments(coins_file=self.write(['SOL'])),['SOL-USDT-SWAP'])
        self.assertEqual(load_instruments(),['SEI-USDT-SWAP'])

    def test_invalid_configs_and_conflicting_inputs(self):
        for value in ([],[''],[2],{'coins':['BTC'],'unexpected':1},'BTC',['BTC']*21):
            with self.assertRaises(ValueError):load_instruments(coins_file=self.write(value))
        with self.assertRaises(ValueError):load_instruments('BTC',self.write(['ETH']))
        self.file.write_text('{bad')
        with self.assertRaises(ValueError):load_instruments(coins_file=str(self.file))

    def test_failed_coin_does_not_stop_next_and_logs_are_separate(self):
        args=SimpleNamespace(output=str(Path(self.tmp.name)/'signals.jsonl'),json=False)
        def fake(collector,service,inst,args):
            if inst.startswith('ETH'):raise RuntimeError('sk-secret-not-to-log')
            return {'symbol':inst.split('-')[0],'instrument':inst,'time':'2026-10-02T00:00:00+00:00','signal':'LONG',
                'ai':{'status':'observed'}}
        with patch('run.scan',side_effect=fake),patch('run.display'),patch('builtins.print'):
            reports=scan_batch(Mock(),Mock(),['BTC-USDT-SWAP','ETH-USDT-SWAP','SEI-USDT-SWAP'],args)
        self.assertEqual([r['signal'] for r in reports],['LONG','WAIT','LONG'])
        lines=Path(args.output).read_text().splitlines()
        self.assertEqual(len(lines),3)
        self.assertNotIn('sk-secret',''.join(lines))
        self.assertEqual(len({r['batch_id'] for r in reports}),1)

    def test_watch_repeats_complete_batch(self):
        p=self.write(['BTC','SEI'])
        with patch('run.Service'),patch('run.Collector'),patch('run.scan_batch') as batch,patch('run.time.sleep',side_effect=[None,KeyboardInterrupt]),patch('builtins.print'):
            main(['--coins',p,'--watch'])
        self.assertEqual(batch.call_count,2)
        self.assertEqual(batch.call_args.args[2],['BTC-USDT-SWAP','SEI-USDT-SWAP'])

if __name__=='__main__':unittest.main()
