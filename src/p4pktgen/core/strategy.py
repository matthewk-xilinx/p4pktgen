from collections import defaultdict
import time
import logging

from p4pktgen.config import Config
from p4pktgen.core.test_cases import TestPathResult, record_test_case
from p4pktgen.util.graph import GraphVisitor, VisitResult
from p4pktgen.util.statistics import Statistics
from p4pktgen.hlir.transition import ParserErrorTransition


def record_path_result(result, is_complete_control_path):
    if result != TestPathResult.SUCCESS or is_complete_control_path:
        return True
    return False


def ge_than_not_none(lhs, rhs):
    if rhs is None or lhs is None:
        return False
    return lhs >= rhs


class ParserGraphVisitor(GraphVisitor):
    def __init__(self, hlir):
        super(ParserGraphVisitor, self).__init__()
        self.hlir = hlir
        self.all_paths = []

    def count(self, stack_counts, state_name):
        if state_name != 'sink':
            state = self.hlir.get_parser_state(state_name)
            for extract in state.header_stack_extracts:
                stack_counts[extract] += 1

    def preprocess_edges(self, path_prefix, onward_edges):
        # Count the number of extractions for each header stack in the path so
        # far.
        stack_counts = defaultdict(int)
        if len(path_prefix) > 0:
            self.count(stack_counts, path_prefix[0].src)
            for e in path_prefix:
                self.count(stack_counts, e.dst)

        # Check whether the path so far involves an extraction beyond the end
        # of a header stack.  In this case, the only legal onward transitions
        # are error transitions.  If there are no such transitions, the
        # returned list will be empty, which will cause the caller to drop the
        # current path-prefix entirely.
        if any(self.hlir.get_header_stack(stack).size < count
               for stack, count in stack_counts.iteritems()):
            return [edge for edge in onward_edges
                    if isinstance(edge, ParserErrorTransition)]

        # Otherwise, no further filtering is necessary.
        return list(onward_edges)

    def visit(self, path, is_complete_path):
        if is_complete_path:
            self.all_paths.append(path)
        return VisitResult.CONTINUE

    def backtrack(self):
        pass


class ControlGraphVisitor(GraphVisitor):
    def __init__(self, path_solver, table_solver, parser_path, source_info_to_node_name,
                 results, test_case_writer):
        super(ControlGraphVisitor, self).__init__()
        self.path_solver = path_solver
        self.table_solver = table_solver
        self.parser_path = parser_path
        self.source_info_to_node_name = source_info_to_node_name
        self.results = results
        self.test_case_writer = test_case_writer
        self.success_path_count = 0

    def generate_test_case(self, control_path, is_complete_control_path):
        expected_path = list(
            self.path_solver.translator.expected_path(self.parser_path,
                                                      control_path)
        )
        path_id = self.path_solver.path_id

        logging_str = "%d Exp path (len %d+%d=%d) complete_path %s: %s" % \
            (path_id, len(self.parser_path), len(control_path),
             len(self.parser_path) + len(control_path),
             is_complete_control_path, expected_path)
        logging.info("")
        logging.info("BEGIN %s" % logging_str)

        time2 = time.time()
        self.path_solver.add_path_constraints(control_path)
        time3 = time.time()

        result = self.path_solver.try_quick_solve(control_path, is_complete_control_path)
        if result == TestPathResult.SUCCESS:
            assert not (record_path_result(result, is_complete_control_path)
                        or record_test_case(result, is_complete_control_path))
            # Path trivially found to be satisfiable and not complete.
            # No test cases required.
            logging.info("Path trivially found to be satisfiable and not complete.")
            logging.info("END   %s" % logging_str)
            return result

        results = []
        extract_vl_variation = Config().get_extract_vl_variation()
        max_test_cases = Config().get_num_test_cases()
        max_path_test_cases = Config().get_max_test_cases_per_path()
        do_consolidate_tables = Config().get_do_consolidate_tables()

        # TODO: Remove this once these two options are made compatible
        assert not (do_consolidate_tables and max_path_test_cases != 1)

        while True:
            self.path_solver.solve_path()

            # Choose values for randomization variables.
            random_constraints = []
            fix_random = is_complete_control_path
            if fix_random:
                self.path_solver.push()
                random_constraints = self.path_solver.fix_random_constraints()

            time4 = time.time()

            result, test_case, packet_list = self.path_solver.generate_test_case(
                expected_path=expected_path,
                parser_path=self.parser_path,
                control_path=control_path,
                is_complete_control_path=is_complete_control_path,
                source_info_to_node_name=self.source_info_to_node_name,
            )
            time5 = time.time()

            # Clear the constraints on the values of the randomization
            # variables.
            if fix_random:
                self.path_solver.pop()

            results.append(result)
            # If this result wouldn't be recorded, subsequent ones won't be
            # either, so move on.
            if not record_test_case(result, is_complete_control_path):
                break

            if do_consolidate_tables:
                # TODO: refactor path_solver to allow extraction of result &
                # record_test_case without building test case.
                self.table_solver.add_path(
                    path_id, self.path_solver.constraints + [random_constraints],
                    self.path_solver.current_context(),
                    self.path_solver.sym_packet,
                    expected_path, self.parser_path, control_path,
                    is_complete_control_path
                )
                break

            test_case["time_sec_generate_ingress_constraints"] = time3 - time2
            test_case["time_sec_solve"] = time4 - time3
            test_case["time_sec_simulate_packet"] = time5 - time4

            # Doing file writing here enables getting at least
            # some test case output data for p4pktgen runs that
            # the user kills before it completes, e.g. because it
            # takes too long to complete.
            self.test_case_writer.write(test_case, packet_list)
            Statistics().num_test_cases += 1
            logging.info("Generated %d test cases for path" % len(results))

            # If we have produced enough test cases overall, enough for this
            # path, or have exhausted possible packets for this path, move on.
            # Using '!=' rather than '<' here as None/0 represents no maximum.
            if Statistics().num_test_cases == max_test_cases \
                    or len(results) == max_path_test_cases \
                    or result == TestPathResult.NO_PACKET_FOUND:
                break

            if not self.path_solver.constrain_last_extract_vl_lengths(extract_vl_variation):
                # Special case: unbounded numbers of test cases are only
                # safe when we're building up constraints on VL-extraction
                # lengths, or else we'll loop forever.
                if max_path_test_cases == 0:
                    break

        # Take result of first loop.
        result = results[0]

        if not Config().get_incremental():
            self.path_solver.solver.reset()

        logging.info("END   %s: %s" % (logging_str, result) )
        return result

    def record_stats(self, control_path, is_complete_control_path, result):
        if result == TestPathResult.SUCCESS and is_complete_control_path:
            Statistics().avg_full_path_len.record(
                len(self.parser_path + control_path))
            for e in control_path:
                if Statistics().stats_per_control_path_edge[e] == 0:
                    Statistics().num_covered_edges += 1
                Statistics().stats_per_control_path_edge[e] += 1
        if result == TestPathResult.NO_PACKET_FOUND:
            Statistics().avg_unsat_path_len.record(
                len(self.parser_path + control_path))
            Statistics().count_unsat_paths.inc()

        if Config().get_record_statistics():
            Statistics().record(result, is_complete_control_path, self.path_solver)

        if record_path_result(result, is_complete_control_path):
            path = (tuple(self.parser_path), tuple(control_path))
            if path in self.results and self.results[path] != result:
                logging.error("result_path %s with result %s"
                              " is already recorded in results"
                              " while trying to record different result %s"
                              "" % (path,
                                    self.results[path], result))
                #assert False
            self.results[path] = result
            if result == TestPathResult.SUCCESS and is_complete_control_path:
                now = time.time()
                self.success_path_count += 1
                # Use real time to avoid printing these details
                # too often in the output log.
                if now - Statistics(
                ).last_time_printed_stats_per_control_path_edge >= 30:
                    Statistics().log_control_path_stats(
                        Statistics().stats_per_control_path_edge,
                        Statistics().num_control_path_edges)
                    Statistics(
                    ).last_time_printed_stats_per_control_path_edge = now
            Statistics().stats[result] += 1


    def visit_result(self, result):
        if ge_than_not_none(Statistics().num_test_cases,
                            Config().get_num_test_cases()):
            return VisitResult.ABORT

        if ge_than_not_none(self.success_path_count,
                            Config().get_max_paths_per_parser_path()):
           return VisitResult.BACKTRACK

        if result != TestPathResult.SUCCESS:
            return VisitResult.BACKTRACK

        return VisitResult.CONTINUE


class PathCoverageGraphVisitor(ControlGraphVisitor):
    def preprocess_edges(self, _path, edges):
        return edges

    def visit(self, control_path, is_complete_control_path):
        self.path_solver.push()
        result = self.generate_test_case(control_path, is_complete_control_path)
        self.record_stats(control_path, is_complete_control_path, result)
        return self.visit_result(result)

    def backtrack(self):
        self.path_solver.pop()



class EdgeCoverageGraphVisitor(ControlGraphVisitor):
    def __init__(self, path_solver, table_solver, parser_path, source_info_to_node_name,
                 results, test_case_writer, graph):
        super(EdgeCoverageGraphVisitor, self).__init__(
            path_solver, table_solver, parser_path, source_info_to_node_name,
            results, test_case_writer
        )
        self.graph = graph
        self.done_edges = set()  # {edge}
        self.edge_visits = defaultdict(int)  # {edge: visit_count}

    def preprocess_edges(self, _path, edges):
        # List non-done edges first, then done edges, with each group sorted by
        # absolute visit count.
        done_edges = []
        non_done_edges = []
        for e in edges:
            l = done_edges if e in self.done_edges else non_done_edges
            l.append(e)
        least_visits_order = \
            sorted(non_done_edges, key=lambda e: self.edge_visits[e]) + \
            sorted(done_edges, key=lambda e: self.edge_visits[e])

        # List is added to a LIFO stack, so reverse the list.
        return reversed(least_visits_order)

    def visit(self, control_path, is_complete_control_path):
        self.path_solver.push()

        # Skip any path that leads to a done branch and who's edges have already
        # all been visited.
        if control_path[-1] in self.done_edges \
                and all(self.edge_visits[e] > 0 for e in control_path):
            return VisitResult.BACKTRACK

        result = self.generate_test_case(control_path, is_complete_control_path)
        self.record_stats(control_path, is_complete_control_path, result)

        # Only increment counts and done edges if a non-error test case was
        # generated.  We want successful test cases in order to consider an edge
        # visited, or done.
        if record_test_case(result, is_complete_control_path) \
                and result == TestPathResult.SUCCESS:
            assert is_complete_control_path
            # Increment visit counts
            for edge in control_path:
                self.edge_visits[edge] += 1

            # Mark final edge as done
            self.done_edges.add(control_path[-1])

            # Mark all edges along graph with all child edges done as done.
            for edge in reversed(control_path[:-1]):
                child_edges = self.graph.get_neighbors(edge.dst)
                if all(ce in self.done_edges for ce in child_edges):
                    self.done_edges.add(edge)

        return self.visit_result(result)

    def backtrack(self):
        self.path_solver.pop()

