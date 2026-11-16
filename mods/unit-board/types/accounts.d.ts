import type { BoardLine } from '../hooks/lines';

/** One board column, as `accounts.py status --json` writes it. */
export interface AccountsBoardColumn {
  key: string;
  heading: string;
  align: 'left' | 'right';
}
/** The cells of one table line, and the same facts for a pane too narrow for the table. */
export interface AccountsBoardRow {
  cells: Record<string, BoardLine>;
  facts: BoardLine[];
}
/** The board rows come from `accounts.status_board()`; the plugin only draws them. */
export interface AccountsBoard {
  columns: AccountsBoardColumn[];
  rows: AccountsBoardRow[];
  /** The reset text holds for one local day; the pane reads the board again from this ISO time. */
  stale_at: string;
}
export interface StoredAccountsStatus {
  board: AccountsBoard;
  problems: string[];
  session_inputs?: string[];
  upcoming_moves?: string[];
}
