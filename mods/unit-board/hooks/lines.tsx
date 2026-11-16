import type { Elements } from 'claude-code';

export type PaneElements = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>;
