# -*- coding: utf-8 -*-
import unittest

from wb_logist.autodist.allocate import allocate_pool, compute_needs
from wb_logist.autodist.ozon_clusters import ClusterStock, distribute_ozon_qty
from wb_logist.autodist.pipeline import is_autodist_command, parse_add_skus
from wb_logist.autodist.velocity import build_signal, compute_velocity, estimate_days_in_stock


class VelocityTest(unittest.TestCase):
    def test_oos_trims_tail(self):
        daily = [2, 2, 0, 0, 0]
        days = estimate_days_in_stock(5, daily_orders=daily, stock_now=0)
        self.assertEqual(days, 2.0)

    def test_velocity(self):
        v, adj = compute_velocity(10, 5, season_factor=1.2)
        self.assertAlmostEqual(v, 2.0)
        self.assertAlmostEqual(adj, 2.4)


class AllocateTest(unittest.TestCase):
    def test_fast_sku_first(self):
        signals = [
            build_signal('slow', clean_orders=5, window_days=10, stock_mp=0),
            build_signal('fast', clean_orders=40, window_days=10, stock_mp=0),
        ]
        needs = compute_needs(signals, cover_ceiling=42)
        result = allocate_pool(needs, allocatable=50)
        by = {i.vendor_code: i for i in result.items}
        self.assertGreater(by['fast'].ship, 0)
        self.assertEqual(result.total_ship, 50)
        # fast should get filled before slow when pool tight
        self.assertGreaterEqual(by['fast'].ship, by['slow'].ship)

    def test_forced_sku(self):
        signals = [build_signal('a', clean_orders=10, window_days=10, stock_mp=1000)]
        needs = compute_needs(signals, forced_skus={'new-sku'})
        codes = {n.vendor_code for n in needs}
        self.assertIn('new-sku', codes)


class OzonClustersTest(unittest.TestCase):
    def test_skip_weak(self):
        clusters = [
            ClusterStock('need-me', stock=1, velocity=1.0),
            ClusterStock('weak', stock=100, velocity=0.1),
            ClusterStock('zero-demand', stock=0, velocity=0.0),
        ]
        ships = distribute_ozon_qty(20, clusters, min_cover_days=14, target_cover_days=28)
        names = {s.name for s in ships}
        self.assertIn('need-me', names)
        self.assertNotIn('zero-demand', names)

    def test_explain_clusters(self):
        from wb_logist.autodist.ozon_clusters import explain_cluster_decisions

        clusters = [
            ClusterStock('need-me', stock=1, velocity=1.0),
            ClusterStock('zero-demand', stock=0, velocity=0.0),
        ]
        decisions = explain_cluster_decisions(
            clusters, min_cover_days=14, assigned_qty={'need-me': 5}
        )
        by = {d['name']: d for d in decisions}
        self.assertTrue(by['need-me']['included'])
        self.assertEqual(by['need-me']['qty'], 5)
        self.assertFalse(by['zero-demand']['included'])
        self.assertIn('исключён', by['zero-demand']['reason'])


class CommandTest(unittest.TestCase):
    def test_parse(self):
        self.assertTrue(is_autodist_command('считай распред'))
        self.assertEqual(
            parse_add_skus('считай распред, добавь sw-a, sw-b'),
            ['sw-a', 'sw-b'],
        )


if __name__ == '__main__':
    unittest.main()
