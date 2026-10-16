"""Regression coverage for slow pair-wide CoinDCX trade-history queries."""
import unittest
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.exchange import Exchange,ExchangeError
from scanner.short_tp import replay_short

PAIR='B-CFX_USDT'
SINCE=1791703000


def ledger(oid,**extra):
    return {'pair':PAIR,'parent_type':'Derivatives::Futures::Order','parent_id':oid,
            'stage':'default','created_at':(SINCE+10)*1000,**extra}


def fill(oid,side='sell',qty='126',price='.07112',offset=10,**extra):
    return {'pair':PAIR,'order_id':oid,'side':side,'quantity':qty,'price':price,
            'timestamp':(SINCE+offset)*1000,**extra}


class TradeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ex=Exchange(Mock(),Mock());self.rows={}
        self.ex.transactions=Mock(return_value=[])
        def pages(path,body):
            self.assertEqual(path,'trades')
            # Reproduce the production failure if a broad query comes back.
            if not body.get('order_id'):raise ExchangeError('ReadTimeout','trades')
            return iter(self.rows.get(body['order_id'],[[]]))
        self.ex.pages=Mock(side_effect=pages)

    def test_reads_each_ledger_order_without_any_pair_wide_query(self):
        self.ex.transactions.return_value=[ledger('entry'),ledger('tp')]
        self.rows={'entry':[[fill('entry')]],'tp':[[fill('tp','buy','94','.06614',20)]]}
        rows=self.ex.trade_fills(PAIR,SINCE)
        self.assertEqual(replay_short(rows,{'entry'},{'tp'}),(D(32),D('.07112')))
        self.assertEqual(self.ex.pages.call_count,2)
        for call in self.ex.pages.call_args_list:
            body=call.args[1]
            self.assertEqual(body['pair'],PAIR);self.assertEqual(body['from_date'],'2026-10-11')
            self.assertIn('to_date',body)

    def test_manual_addition_discovered_without_being_in_known_ids(self):
        self.ex.transactions.return_value=[ledger('entry'),ledger('manual'),ledger('tp')]
        self.rows={'entry':[[fill('entry')]],'tp':[[fill('tp','buy','94','.06614',20)]],
                   'manual':[[fill('manual','sell','32','.095',30)]]}
        entries=self.ex.recent_entries(PAIR,'SELL',SINCE,{'entry','tp'})
        self.assertEqual({e['id'] for e in entries},{'entry','manual'})
        rows=self.ex.trade_fills(PAIR,SINCE,{'entry','tp'})
        self.assertEqual(replay_short(rows,{'entry','manual'},{'tp'}),(D(64),D('.08306')))

    def test_unrecognized_manual_close_is_not_hidden_by_known_ids(self):
        self.ex.transactions.return_value=[ledger('entry'),ledger('manual-close')]
        self.rows={'entry':[[fill('entry')]],'manual-close':[[fill('manual-close','buy','10','.06',20)]]}
        rows=self.ex.trade_fills(PAIR,SINCE,{'entry'})
        with self.assertRaisesRegex(ValueError,'UNATTRIBUTED_SHORT_REDUCTION'):
            replay_short(rows,{'entry'},set())

    def test_known_orders_cover_ledger_publication_lag(self):
        self.rows={'entry':[[fill('entry')]],'tp':[[fill('tp','buy','94','.06614',20)]]}
        rows=self.ex.trade_fills(PAIR,SINCE,{'entry','tp',None})
        self.assertEqual(replay_short(rows,{'entry'},{'tp'}),(D(32),D('.07112')))

    def test_partial_fills_across_pages_and_orders_are_preserved(self):
        self.ex.transactions.return_value=[ledger('manual')]
        self.rows={'manual':[[fill('manual','buy','2','1')],[fill('manual','buy','3','2',20)]]}
        entries=self.ex.recent_entries(PAIR,'BUY',SINCE)
        self.assertEqual(len(entries),1)
        self.assertEqual((entries[0]['total_quantity'],entries[0]['avg_price']),('5','1.6'))

    def test_duplicate_ledger_records_and_known_ids_read_order_once(self):
        self.ex.transactions.return_value=[ledger('entry'),ledger('entry')]
        self.rows={'entry':[[fill('entry')]]}
        self.assertEqual(len(self.ex.trade_fills(PAIR,SINCE,{'entry'})),1)
        self.ex.pages.assert_called_once()

    def test_filters_other_coins_funding_nonorders_and_earlier_episodes(self):
        self.ex.transactions.return_value=[ledger('entry'),ledger('gold',pair='B-XAU_USDT'),
            ledger('funding',stage='funding'),ledger('transfer',parent_type='Transfer'),
            ledger('old',created_at=(SINCE-1)*1000)]
        self.rows={'entry':[[fill('entry',offset=-1),fill('entry')]]}
        self.assertEqual(len(self.ex.trade_fills(PAIR,SINCE)),1)
        self.ex.pages.assert_called_once()

    def test_known_pending_order_without_fills_is_valid(self):
        self.assertEqual(self.ex.trade_fills(PAIR,SINCE,{'pending'}),[])

    def test_missing_fills_for_recorded_execution_does_not_silently_succeed(self):
        self.ex.transactions.return_value=[ledger('filled')]
        with self.assertRaisesRegex(ExchangeError,'not yet available'):self.ex.trade_fills(PAIR,SINCE)

    def test_ledger_failure_cannot_return_only_known_fills(self):
        self.ex.transactions.side_effect=ExchangeError('ReadTimeout','positions/transactions')
        with self.assertRaises(ExchangeError):self.ex.trade_fills(PAIR,SINCE,{'entry'})
        self.ex.pages.assert_not_called()

    def test_order_failure_or_incomplete_pagination_cannot_return_partial_history(self):
        self.ex.transactions.return_value=[ledger('entry')]
        def pages(*args):
            yield [fill('entry')]
            raise ExchangeError('ReadTimeout','trades')
        self.ex.pages.side_effect=pages
        with self.assertRaises(ExchangeError):self.ex.trade_fills(PAIR,SINCE)

    def test_unexpected_pair_or_order_in_response_blocks_history(self):
        self.ex.transactions.return_value=[ledger('entry')]
        for extra in ({'pair':'B-XAU_USDT'},{'order_id':'different'}):
            self.rows={'entry':[[{**fill('entry'),**extra}]]}
            with self.assertRaisesRegex(ExchangeError,'ownership mismatch'):self.ex.trade_fills(PAIR,SINCE)

    def test_malformed_ledger_order_id_fails_before_trade_read(self):
        self.ex.transactions.return_value=[ledger(None)]
        with self.assertRaisesRegex(ExchangeError,'Missing order ID'):self.ex.trade_fills(PAIR,SINCE)
        self.ex.pages.assert_not_called()

    def test_gold_rejected_before_any_private_read(self):
        with self.assertRaises(ValueError):self.ex.trade_fills('B-XAU_USDT',SINCE)
        self.ex.transactions.assert_not_called();self.ex.pages.assert_not_called()


class TransactionPaginationTests(unittest.TestCase):
    def setUp(self):
        self.ex=Exchange(Mock(),Mock())
        self.ex.request=Mock()
        self.sleep=patch('scanner.exchange.time.sleep')
        self.sleep.start();self.addCleanup(self.sleep.stop)

    def page(self,start,count=100):
        return [ledger('tx-'+str(i),created_at=(SINCE+i)*1000)
                for i in range(start,start-count,-1)]

    def test_stable_history_stops_at_cutoff_and_verifies_head(self):
        head=self.page(200);middle=self.page(100);old=self.page(-1)
        self.ex.request.side_effect=[head,middle,old,head]
        rows=self.ex.transactions(SINCE+50)
        self.assertEqual(len(rows),151)
        self.assertEqual([c.args[1]['page'] for c in self.ex.request.call_args_list],['1','2','3','1'])
        self.assertTrue(all(c.args[1]['stage']=='all' for c in self.ex.request.call_args_list))

    def test_funding_insertion_inversion_restarts_instead_of_crawling_100_pages(self):
        head=self.page(200);new_head=self.page(205)
        shifted=self.page(105)
        self.ex.request.side_effect=[head,shifted,new_head,shifted,self.page(5),new_head]
        rows=self.ex.transactions(SINCE+50)
        self.assertEqual(len(rows),156)
        self.assertEqual(len({r['parent_id'] for r in rows}),len(rows))
        self.assertEqual([c.args[1]['page'] for c in self.ex.request.call_args_list],['1','2','1','2','3','1'])

    def test_single_row_shift_without_inversion_cannot_double_count_profit(self):
        head=self.page(200);new_head=self.page(201);shifted=self.page(101);old=self.page(1)
        self.ex.request.side_effect=[head,shifted,old,new_head,new_head,shifted,old,new_head]
        rows=self.ex.transactions(SINCE+50)
        self.assertEqual(len(rows),152)
        self.assertEqual(len({r['parent_id'] for r in rows}),len(rows))
        self.assertEqual(self.ex.request.call_count,8)

    def test_equal_timestamps_shift_is_detected_by_head_verification(self):
        head=[ledger('first')];changed=[ledger('new'),ledger('first')]
        self.ex.request.side_effect=[head,changed,changed,changed]
        self.assertEqual([r['parent_id'] for r in self.ex.transactions(SINCE)],['new','first'])

    def test_continuously_changing_history_fails_bounded_without_partial_rows(self):
        self.ex.request.side_effect=[self.page(200),self.page(105)]*3
        with self.assertRaisesRegex(ExchangeError,'kept changing'):
            self.ex.transactions(SINCE)
        self.assertEqual(self.ex.request.call_count,6)

    def test_persistent_unsorted_page_is_rejected_without_ignoring_cutoff(self):
        self.ex.request.return_value=[ledger('old',created_at=(SINCE-1)*1000),ledger('new')]
        with self.assertRaisesRegex(ExchangeError,'kept changing'):
            self.ex.transactions(SINCE)
        self.assertEqual(self.ex.request.call_count,3)

    def test_empty_history_is_verified_and_new_insert_is_not_lost(self):
        row=[ledger('new')]
        self.ex.request.side_effect=[[],row,row,row]
        self.assertEqual(self.ex.transactions(SINCE),row)

    def test_funding_exits_and_currency_filter_are_preserved(self):
        rows=[ledger('entry'),ledger('funding',stage='funding'),
              ledger('exit',stage='tpsl_exit'),ledger('inr',margin_currency_short_name='INR')]
        self.ex.request.return_value=rows
        self.assertEqual([r['parent_id'] for r in self.ex.transactions(SINCE)],['entry','funding','exit'])

    def test_malformed_timestamp_cannot_make_a_snapshot_look_complete(self):
        for stamp in (None,'nan','inf','bad'):
            with self.subTest(stamp=stamp):
                self.ex.request.return_value=[ledger('bad',created_at=stamp)]
                with self.assertRaisesRegex(ExchangeError,'Invalid transaction timestamp'):
                    self.ex.transactions(SINCE)

    def test_read_failure_is_not_masked_by_partial_results(self):
        self.ex.request.side_effect=[self.page(200),ExchangeError('ReadTimeout','positions/transactions')]
        with self.assertRaisesRegex(ExchangeError,'ReadTimeout'):
            self.ex.transactions(SINCE)

    def test_genuine_page_limit_still_blocks_incomplete_history(self):
        self.ex.request.side_effect=[self.page(20000-100*i) for i in range(100)]
        with self.assertRaisesRegex(ExchangeError,'Incomplete pagination'):
            self.ex.transactions(SINCE)
        self.assertEqual(self.ex.request.call_count,100)
