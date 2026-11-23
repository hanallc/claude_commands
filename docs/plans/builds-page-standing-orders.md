# Builds page: nate's standing orders

admin-console keeps this list. bob sends each new order of nate to admin-console, which adds it here.
Before the merge of each Builds page change, check every line on a page with real queue data.
Each line names the page test that proves it.

| # | Date (PST) | nate's words or approved design | Test that proves it |
| --- | --- | --- | --- |
| 1 | 11-21 17:05 | Approved mockup `bob/designs/queue-rows/after.png`: the unit starts the row, the showrunner is the small text under it, Step comes after Verify. | `test_memory_hover.MemoryHoverTests.test_queue_rows_show_unit_showrunner_step_and_one_running_target` |
| 2 | 11-23 | "the unit is listed at the beginning of the row and it should show in the color of units and and be removed as a column on the right because it is REDUNDANT" | `test_memory_hover.MemoryHoverTests.test_queue_rows_show_unit_showrunner_step_and_one_running_target`; `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 3 | 11-23 | "where it is running should be what shows in the running column - the target can be in the tool tip but this is now useless - compeltely useless - for me to know at a glance where something is running" | `test_memory_feed.MemoryFeedTests.test_running_gate_target_cell_shows_only_the_running_machine`; `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 4 | 11-23 10:00 | No Slot column: nate asked why it is still there. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 5 | 11-23 11:46 | "i want the icons / emoji's for the machine's to be fast tracked": bob's proposal `bob/designs/machine-icons/mock.html`, with the machine names in the tooltip and a legend. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 6 | 11-23 12:17 | A showrunner line under each row title. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 7 | 11-23 12:17 | No wrapped row title. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 8 | 11-23 12:17 | The table text is in line with the first line of the row title. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 9 | 11-23 12:15 | "i see the ablity to reorder steps on the build page": a reorder handle on each Waiting row. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 10 | 11-23 12:44 | Form with a reason (bob's check): a waiting row that the queue cannot move has no reorder handle. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 11 | 11-23 12:44 | Form with a reason (bob's check): a Mac-only waiting step has no memory box, because the chart shows natedev memory. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
| 12 | 11-23 12:44 | Form with a reason (bob's check): a showrunner's own run has no small line, because the line would repeat the owner. | `test_memory_feed.BuildsPageProofTests.test_builds_page_renders_the_queue_and_tooltips_in_chrome` (`checkStandingOrders`) |
