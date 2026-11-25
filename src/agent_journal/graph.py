"""Draw session lines over a chronological page of journey rows."""


def graph(rows):
    by_key = {(row["thread"], row["turn"]): i for i, row in enumerate(rows)}
    first, last, forks = {}, {}, {}
    for index, row in enumerate(rows):
        session = row["thread"]
        if session not in first:
            first[session] = index
            anchor = by_key.get((row["parent"], row["parent_turn"]))
            if anchor is not None and anchor < index:
                forks[session] = anchor
        last[session] = index

    starts = {session: forks.get(session, index) for session, index in first.items()}
    lane_ends, lanes = [], {}
    for session in sorted(starts, key=starts.get):
        start = starts[session]
        lane = next(
            (i for i, end in enumerate(lane_ends) if end < start), len(lane_ends)
        )
        if lane == len(lane_ends):
            lane_ends.append(last[session])
        else:
            lane_ends[lane] = last[session]
        lanes[session] = lane

    lines = []
    for index, row in enumerate(rows):
        cells = [" "] * (2 * len(lane_ends))
        for session, lane in lanes.items():
            if starts[session] <= index <= last[session]:
                cells[lane * 2] = "│"
        point = lanes[row["thread"]] * 2
        cells[point] = "●"
        for session, anchor in forks.items():
            if anchor != index:
                continue
            branch = lanes[session] * 2
            for i in range(min(point, branch) + 1, max(point, branch)):
                cells[i] = "┼" if cells[i] == "│" else "─"
            cells[branch] = "┐" if point < branch else "┌"
        lines.append("".join(cells).rstrip())
    return lines
