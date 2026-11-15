---
description: Show an image to the user on the console Images page.
argument-hint: "<image path> <caption>"
---

# Show image

Use this when the user must look at an image: before and after shots of a page change, a screen defect, or a chart.
Register the image on the console Images page and give the user the image's own link.
A file path alone is not something the user can view.

An image for the user's review is one dark mode shot at desktop width.
Send a light mode or phone width shot only when the user asks for it.
Check the light mode and phone width yourself when the change needs them.

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
4. Give the user the `Image link:` that the command prints for each image.
   It opens the Images page at that image.
   In a notice to a showrunner, give the same links and the image ids.

The page shows the oldest image first, and a new image goes to the end.
The user answers on an image with **Approved**, **Not Approved** or **Send Feedback**, and any of them can carry feedback.
The answer arrives as a message that starts `From the user (via the Images page):`.
Act on it as on the user's words. Then delete the image with the command in the message.
To remove your own image, run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id <id>`.
