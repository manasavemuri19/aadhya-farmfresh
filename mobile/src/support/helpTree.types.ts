export interface HelpTreeNode {
  id: string;
  text: string;
  /** Present only on branching nodes — a leaf (no options) automatically
   * gets the "still stuck?" contact + ticket prompt below its answer. */
  options?: { label: string; next: string }[];
}

export type HelpTree = Record<string, HelpTreeNode>;
