import time
import unittest

from rom_pool import EnginePool


class FakeWorker:
    is_running = True

    def close(self):
        self.is_running = False


class PoolTest(unittest.TestCase):
    def test_prewarm_and_reuse_three_checkpoints(self):
        built = []

        def factory(_deadline):
            item = (FakeWorker(), 0x02000000 + len(built))
            built.append(item)
            return item

        pool = EnginePool(factory, size=3)
        try:
            pool.prewarm()
            deadline = time.monotonic() + 2
            while len(built) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(built), 3)
            with pool.session(deadline) as first:
                with pool.session(deadline) as second:
                    with pool.session(deadline) as third:
                        self.assertEqual(len({id(first[0]), id(second[0]), id(third[0])}), 3)
            with pool.session(deadline) as reused:
                self.assertIn(reused, built)
            self.assertEqual(len(built), 3)
        finally:
            pool.close()
        self.assertTrue(all(not worker.is_running for worker, _ in built))


if __name__ == "__main__":
    unittest.main()
