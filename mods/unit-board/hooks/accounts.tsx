import { span, wrapLine } from './lines';
import type { BoardLine, PaneElements } from './lines';
import type { AccountsBoard, AccountsBoardColumn, AccountsBoardRow, StoredAccountsStatus } from '../types/accounts';
import { cellWidth } from './geometry';

interface TableColumn extends AccountsBoardColumn {
  width: number;
}
const cell = (row: AccountsBoardRow, key: string): BoardLine => row.cells[key] ?? [];

const lineWidth = (line: BoardLine) => line.reduce((width, part) => width + cellWidth(part.text), 0);
const joinFacts = (facts: BoardLine[]): BoardLine => facts.flatMap((fact, index) =>
  index ? [span(' · ', 'inactive'), ...fact] : fact);

/** Keep facts intact, dropping only the separator where a new row starts. */
function wrapFacts(facts: BoardLine[], width: number): BoardLine[] {
  const indent = Math.max(0, Math.min(2, width - 1));
  const room = width - indent;
  const lines: BoardLine[] = [];
  let current: BoardLine[] = [];
  let used = 0;
  const flush = () => {
    if (current.length) lines.push([span(' '.repeat(indent)), ...joinFacts(current)]);
    current = [];
    used = 0;
  };
  for (const fact of facts) {
    const size = lineWidth(fact);
    if (size > room) {
      flush();
      lines.push(...wrapLine(fact, width, indent));
      continue;
    }
    const separator = current.length ? cellWidth(' · ') : 0;
    if (used + separator + size > room) flush();
    used += (current.length ? cellWidth(' · ') : 0) + size;
    current.push(fact);
  }
  flush();
  return lines;
}

function tableColumns(board: AccountsBoard): TableColumn[] {
  return board.columns.map(column => ({ ...column, width: board.rows.reduce(
    (width, row) => Math.max(width, lineWidth(cell(row, column.key))), cellWidth(column.heading)) }));
}

function tableLine(columns: TableColumn[], cells: Record<string, BoardLine>, indent = 0): BoardLine {
  const line = [span(' '.repeat(indent)), ...columns.flatMap((column, index) => {
    const content = cells[column.key] ?? [];
    const padding = span(' '.repeat(column.width - lineWidth(content)));
    return [...(index ? [span('  ')] : []),
      ...(column.align === 'right' ? [padding, ...content]
        : index === columns.length - 1 ? content : [...content, padding])];
  })];
  // Missing final cells leave no padding at the row's edge.
  while (line.length) {
    const last = line[line.length - 1]!;
    const text = last.text.trimEnd();
    if (text) {
      line[line.length - 1] = { ...last, text };
      break;
    }
    line.pop();
  }
  return line;
}

/** The full table, then two tables split before `resets`, then each row's facts. */
function accountLines(board: AccountsBoard, width: number): BoardLine[] {
  if (!board.rows.length) return [];
  const columns = tableColumns(board);
  const header = Object.fromEntries(columns.map(column => [column.key, [span(column.heading, 'default', 'bold')]]));
  const tableWidth = (columns: TableColumn[], indent = 0) =>
    indent + columns.reduce((width, column) => width + column.width, 0) + (columns.length - 1) * 2;
  if (tableWidth(columns) <= width) {
    return [tableLine(columns, header), ...board.rows.map(row => tableLine(columns, row.cells))];
  }
  const split = columns.findIndex(column => column.key === 'resets');
  const first = columns.slice(0, split);
  const second = columns.slice(split);
  if (split > 0 && tableWidth(first) <= width && tableWidth(second, 2) <= width) {
    return [tableLine(first, header), tableLine(second, header, 2),
      ...board.rows.flatMap(row => [tableLine(first, row.cells),
        ...(cell(row, 'label').length ? [tableLine(second, row.cells, 2)] : [])])];
  }
  return board.rows.flatMap(row => {
    const dot = cell(row, 'dot');
    const heading = [...dot, ...(dot.length ? [span(' ')] : []), ...cell(row, 'label')];
    return [...(heading.length ? wrapLine(heading, width) : []), ...wrapFacts(row.facts, width)];
  });
}

export function AccountsView(elements: PaneElements, columns: number, status: StoredAccountsStatus) {
  const { Box, Text } = elements;
  const width = Math.max(1, Math.floor(columns));
  const rows = accountLines(status.board, width);
  const drawLine = (parts: BoardLine) => <Box flexDirection="row" flexShrink={0}>
    {(parts.some(part => part.text) ? parts.filter(part => part.text) : [span('')]).map(part => part.tone === 'default'
      ? <Text bold={part.kind === 'bold'}>{part.text}</Text>
      : <Text color={part.tone} bold={part.kind === 'bold'}>{part.text}</Text>)}
  </Box>;
  return <Box flexDirection="column">
    {rows.map(drawLine)}
    {status.upcoming_moves && <Box flexDirection="column" marginTop={1}>
      {status.upcoming_moves.flatMap(line => line ? wrapLine([span(line)], width) : [[]]).map(drawLine)}
    </Box>}
    {status.problems.length > 0 && <Box flexDirection="column" marginTop={1}>
      {wrapLine([span('Problems', 'default', 'bold')], width).map(drawLine)}
      {status.problems.flatMap(problem => wrapLine([span(`• ${problem}`, 'warning')], width)).map(drawLine)}
    </Box>}
  </Box>;
}
