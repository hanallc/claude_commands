<IdleUpdate>
Producer, showrunner and unit sessions use idle update mode.
The mode starts when nate typed no prompt in this session for `after_minutes` (`config/idle_update.conf`).
The prompt hook measures this time and tells you the form for each turn in an `Idle update mode:` line.

- Keep doing all your work in the mode.
- Your text to nate is one short update. It ends with the question whether he is willing to look at what is on deck.
- The on-deck line says what the item is about and what he decides, in one or two sentences.
- Each news point says what the thing is in his words, then the news, then the cause.
- Show the item in full only after he says yes. This also holds for a new question: give it as an on-deck line. It overrides each rule that asks a question in full.
- The update renews on the first turn `renew_minutes` or more after the last update. It carries only the news since that update.
- Every other turn in the mode has no text for nate. It ends with "—" alone. A unit director ends it with its one `— holding:`, `gate:`, `decision:`, `blocked:` or `done:` line only, because scripts read that line.
- Use no timer for the update. A timer turn with no news spends tokens.

The mode ends when nate types a prompt in this session.
User, 2026-11-22: "you syould just say what it is you last had on deck with me and ask if i am willing to look at it".
User, 2026-11-22: "i need a little bit of context about each of these in some way - it's not bad but it need 50% more info".
</IdleUpdate>
