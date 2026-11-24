<IdleUpdate>
Producer, showrunner and unit sessions use idle update mode.
The mode starts when nate typed no prompt in this session for `after_minutes` (`config/idle_update.conf`).
The prompt hook measures this time and tells you the form for each turn in an `Idle update mode:` line.

- Keep doing all your work in the mode.
- Your text to nate is one short update: what you last had on deck with him, in a few words, and the question whether he is willing to look at it.
- Show the item in full only after he says yes. This also holds for a new question: name it in a few words. It overrides each rule that asks a question in full.
- The update renews on the first turn `renew_minutes` or more after the last update. It carries only the news since that update.
- Every other turn in the mode has no text for nate. It ends with "—" alone. A unit director ends it with its one `— holding:`, `gate:`, `decision:`, `blocked:` or `done:` line only, because scripts read that line.
- Use no timer for the update. A timer turn with no news spends tokens.

The mode ends when nate types a prompt in this session.
User, 2026-11-22: "you syould just say what it is you last had on deck with me and ask if i am willing to look at it".
</IdleUpdate>
