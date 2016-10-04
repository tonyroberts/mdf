from mdf import (
    MDFContext,
    evalnode,
    datanode,
    run
)

from datetime import datetime
import pandas as pa
import unittest
import logging

# this is necessary to stop namespace from looking
# too far up the stack as it looks for the first frame
# not in the mdf package

__package__ = None

_logger = logging.getLogger(__name__)

@evalnode
def Counter():
    accum = -2.0
    while True:
        if accum != 0.0:
            yield accum
        accum += 0.5

daterange = pa.bdate_range(datetime(1970, 1, 1), datetime(1970, 1, 10))

df_a = pa.DataFrame([{"A": i, "B": -i} for i in range(len(daterange))], index=daterange, dtype=float)
df_b = pa.DataFrame([{"A": -i, "B": i} for i in range(len(daterange))], index=daterange, dtype=float)

df_node_a = datanode("df_a", df_a)
df_node_b = datanode("df_b", df_b)


class OperatorNodeTest(unittest.TestCase):
    def setUp(self):
        self.daterange = daterange
        self.ctx = MDFContext()

    def test_binary_operators_with_constant(self):
        self._test(Counter, [-2.0, -1.5, -1.0, -0.5, 0.5, 1.0, 1.5])
        self._test(Counter + 0.2, [-1.8, -1.3, -0.8, -0.3, 0.7, 1.2, 1.7])
        self._test(Counter - 0.2, [-2.2, -1.7, -1.2, -0.7, 0.3, 0.8, 1.3])
        self._test(Counter * 2.0, [-4.0, -3.0, -2.0, -1.0, 1.0, 2.0, 3.0])
        self._test(Counter / 0.5, [-4.0, -3.0, -2.0, -1.0, 1.0, 2.0, 3.0])

    def test_binary_reverse_operators_with_constant(self):
        self._test(0.2 + Counter, [-1.8, -1.3, -0.8, -0.3, 0.7, 1.2, 1.7])
        self._test(1.0 - Counter, [3.0, 2.5, 2.0, 1.5, 0.5, 0.0, -0.5])
        self._test(2.0 * Counter, [-4.0, -3.0, -2.0, -1.0, 1.0, 2.0, 3.0])
        self._test(12 / (Counter + .25), [-6.8571428571428568, -9.6000000000000014, -16.0,
                                          -48.0, 16.0, 9.6000000000000014, 6.8571428571428568])

    def test_binary_operators_with_node(self):
        self._test(Counter + Counter, [-4.0, -3.0, -2.0, -1.0, 1.0, 2.0, 3.0])
        self._test(Counter - Counter, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self._test(Counter * Counter, [4.0, 2.25, 1.0, 0.25, 0.25, 1.0, 2.25])
        self._test(Counter / Counter, [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])

    def test_get_all_data(self):
        self._run()

        # binop on two datanodes
        actual = self.ctx._get_all_values(df_node_a + df_node_b)
        expected = pa.DataFrame({"A": 0.0, "B": 0.0}, index=self.daterange)
        self.assertTrue((actual == expected).all().all())

        # binop with constant
        actual = self.ctx._get_all_values(df_node_a *10)
        expected = pa.DataFrame([{"A": i*10, "B": -i*10} for i in range(len(self.daterange))], index=self.daterange)
        self.assertTrue((actual == expected).all().all())

        # chained
        actual = self.ctx._get_all_values(df_node_a * df_node_a * 10)
        expected = pa.DataFrame([{"A": i*i*10, "B": i*i*10} for i in range(len(self.daterange))], index=self.daterange)
        self.assertTrue((actual == expected).all().all())

    def _test(self, node, expected_values):
        values = node.queuenode()
        self._run(values)
        actual = self.ctx[values]
        self.assertEquals(list(actual), expected_values)

    def _run_for_daterange(self, date_range, *nodes):
        results = []
        def callback(date, ctx):
            results.append([ctx[node] for node in nodes])
        run(date_range, [callback], ctx=self.ctx)
        return results

    def _run(self, *nodes):
        return self._run_for_daterange(self.daterange, *nodes)
