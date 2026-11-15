---
description: Show an image to the user on the console Images page.
argument-hint: "<image path> <caption>"
---

# Show image

Use this when the user must look at an image: before and after shots of a page change, a screen defect, or a chart.
Register the image on the console Images page and give the user that page's link.
A file path alone is not something the user can view.

## Steps

1. Write a caption. Say what changed and what to look at. Start a proof shot with `Before:` or `After:`.
2. Find your fixed staff id: the `id` in your staff row.
   `~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py get <your worktree path>` prints the row.
3. Register each image:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py register --image <path> --caption "<caption>" --owner <your staff id> (--change <id> | --phase <id>)
   ```
   - Use `--change` for a change review row, such as `sdlc/C12`. Use `--phase` for phase work.
   - The command accepts `.png`, `.jpg`, `.jpeg`, `.gif` and `.webp` files.
   - It copies the image, so you can delete the source file after it prints `registered`.
4. Give the user the `Images page:` link that the command prints.
   In a notice to a showrunner, give the same link and the image ids.

The page shows the oldest image first, and a new image goes to the end.
The user removes an image with its **Delete** button.
To remove your own image, run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id <id>`.
