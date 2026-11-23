"""Unit tests of the minimap's git graph (`branches._graph`), using the README's example history.

Run: python3 -m unittest test.test_graph   (from the repo root)"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh.branches import _graph  # noqa: E402


def U(t):
    return ("user", "", [{"type": "text", "text": t}])


def A(t, tool=False):
    c = [{"type": "text", "text": t}]
    if tool:
        c.append({"type": "tool_use", "id": "x", "name": "spawn", "input": {}})
    return ("assistant", "", c)


TR = ("user", "", [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}])


class Hist:
    """Builds _graph's inputs: messages (id -> parent, meta) and threads (path, origin, kind…)."""

    def __init__(self):
        self.msgs, self.parent, self.threads = {}, {}, []

    def add(self, mid, parent, meta):
        self.msgs[mid] = meta + (mid,)  # created_at = id (time order)
        self.parent[mid] = parent

    def path(self, head):
        out = []
        while head is not None:
            out.append(head)
            head = self.parent[head]
        return out[::-1]

    def thread(self, bid, kind, origin, head, parent=None, ctx="fork", spawned_at=None, created=0):
        self.threads.append(dict(id=bid, kind=kind, origin_id=origin, head=head, parent=parent, context_mode=ctx,
                                 spawned_at_id=spawned_at if spawned_at is not None else origin, created_at=created))

    def graph(self, main):
        paths, own, pb, sums = {}, {}, {}, []
        for t in self.threads:
            p = self.path(t["head"])
            paths[t["id"]] = p
            own[t["id"]] = set(p[p.index(t["origin_id"]) + 1:]) if t["origin_id"] in p else set(p)
            pb[t["id"]] = t["parent"]
            sums.append(t)
        brows = [(t["id"],) for t in self.threads]
        g = _graph(brows, paths, own, pb, sums, self.msgs, main)
        return g, {n["id"]: n for n in g["nodes"]}


def readme_history():
    h = Hist()
    h.add(1, None, U("Investigate XYZ"))
    h.add(2, 1, A("Summary of XYZ"))
    h.add(3, 2, U("What about consideration ABC"))
    h.add(4, 3, A("How ABC affects XYZ"))
    h.add(5, 4, U("Plan for solving XYZ"))
    h.add(6, 5, A("Here's a plan for solving XYZ"))
    h.add(7, 6, U("Iplament solution for XXZ"))      # the edited-away original
    h.add(9, 6, U("Implement solution for XYZ"))     # its replacement
    h.add(10, 9, A("Solution is implemented for XYZ"))
    h.add(11, 10, U("Could we consider a tweak PQR"))
    h.add(12, 10, U("Review implementation for XYZ"))
    h.thread("R", "root", None, 12)
    h.thread("E", "edit", 6, 7, parent="R", created=8)
    h.thread("Q", "aside", 10, 11, parent="R", created=11)
    return h


class TestGraph(unittest.TestCase):
    def test_readme_example(self):
        g, n = readme_history().graph("R")
        self.assertEqual([x["mid"] for x in g["nodes"]], [1, 6, 7, 9, 10, 11, 12])
        self.assertEqual([x["lane"] for x in g["nodes"]], [0, 0, 1, 0, 0, 1, 0])  # lane 1 is reused
        self.assertEqual(n["m6"]["hidden"], 4)  # Summary, ABC, How ABC, Plan are folded away
        self.assertEqual(n["m6"]["parent"], "m1")
        self.assertTrue(n["m9"]["edit"] and not n["m7"]["edit"])
        self.assertTrue(n["m1"]["start"] and n["m7"]["start"] and n["m11"]["start"])
        self.assertFalse(n["m9"]["start"] or n["m12"]["start"] or n["m6"]["start"])
        self.assertEqual((n["m7"]["thread"], n["m11"]["thread"], n["m12"]["thread"]), ("E", "Q", "R"))
        self.assertEqual((n["m7"]["tips"], n["m11"]["tips"], n["m12"]["tips"]), (["E"], ["Q"], ["R"]))
        self.assertEqual(g["main_tip"], "m12")
        self.assertEqual(n["m10"]["preview"], "Solution is implemented for XYZ")

    def test_main_tag_takes_lane_zero(self):
        g, n = readme_history().graph("Q")
        self.assertEqual(g["main_tip"], "m11")
        self.assertEqual([n["m%d" % i]["lane"] for i in (1, 6, 9, 10, 11)], [0] * 5)
        self.assertNotEqual(n["m12"]["lane"], 0)  # the original thread's continuation is now a side line

    def test_subagents_attach_to_the_turn_final_message(self):
        h = readme_history()
        h.add(13, 12, A("spawning", tool=True))
        h.add(20, None, U("subagent task"))  # fresh: no shared history
        h.add(21, 20, A("subagent report"))
        h.thread("S", "subagent", None, 21, parent="R", ctx="fresh", spawned_at=13, created=20)
        h.threads[0]["head"] = 13
        g, n = h.graph("R")
        self.assertEqual(n["m20"]["parent"], "m12")  # parent turn still running: its prompt
        self.assertTrue(n["m20"]["fresh"] and n["m20"]["start"])
        h.add(14, 13, TR)
        h.add(15, 14, A("done, the subagent reported"))
        h.threads[0]["head"] = 15
        g, n = h.graph("R")
        self.assertEqual(n["m20"]["parent"], "m15")  # then its final message
        self.assertGreater(n["m20"]["row"], n["m15"]["row"])
        self.assertEqual(n["m21"]["tips"], ["S"])
        self.assertNotIn("m13", n)  # tool calls are not commits

    def test_second_edit_marks_only_later_versions(self):
        h = readme_history()
        h.add(8, 6, U("Implment solution for XYZ"))  # a second attempt (also edited away)
        h.thread("E2", "edit", 6, 8, parent="R", created=9)
        g, n = h.graph("R")
        self.assertEqual((n["m7"]["edit"], n["m8"]["edit"], n["m9"]["edit"]), (False, True, True))

    def test_aside_from_main_tip_gets_its_own_lane(self):
        h = readme_history()
        h.add(13, 12, A("Reviewed"))
        h.add(14, 13, U("side question"))
        h.thread("Z", "aside", 13, 14, parent="R", created=14)
        h.threads[0]["head"] = 13
        g, n = h.graph("R")
        self.assertEqual(n["m13"]["lane"], 0)
        self.assertNotEqual(n["m14"]["lane"], 0)

    def test_empty_thread_gets_a_placeholder_tip(self):
        h = readme_history()
        h.thread("N", "aside", 6, 6, parent="R", created=30)
        g, n = h.graph("R")
        self.assertEqual(n["t:N"]["parent"], "m6")
        self.assertEqual(n["t:N"]["tips"], ["N"])
        self.assertEqual(n["t:N"]["hidden"], 0)


if __name__ == "__main__":
    unittest.main()
