---
description: Show an image to the user on the console Images page.
argument-hint: "<image path> <caption> | <before path> <after path> <caption>"
---

# Show image

Use this when the user must look at an image: before and after shots of a page change, a screen defect, or a chart.
Register the image on the console Images page and give the user the image's own link.
A file path alone is not something the user can view.

## Steps

1. Write a caption. Say what changed and what to look at.
   File the before and after shots of a change as one pair. The page labels each image, so the caption needs no `Before:` or `After:`.
2. Find your fixed staff id: the `id` in your staff row.
   `~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py get <your worktree path>` prints the row.
3. Register each image or pair:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py register (--image <path> | --before <path> --after <path>) --caption "<caption>" --owner <your staff id> (--change <id> | --phase <id>)
   ```
   - A pair shows its two images side by side on one card, with one caption, one Feedback field and one Delete. On a narrow window the before image is on top.
   - Use `--change` for a change review row, such as `sdlc/C12`. Use `--phase` for phase work.
   - The command accepts `.png`, `.jpg`, `.jpeg`, `.gif` and `.webp` files.
   - It copies the images, so you can delete the source files after it prints `registered`.
4. Give the user the `Image link:` that the command prints for each image or pair.
   It opens the Images page at that image.
   In a notice to a showrunner, give the same links and the image ids.

The page shows the oldest image first, and a new image goes to the end.
The user removes an image, or both images of a pair, with its **Delete** button.
The user can also send feedback on an image. It arrives as a message that starts `From the user (via the Images page): feedback on your image.`
Act on it as on the user's words. Then delete the image with the command in the message.
To remove your own image, run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id <id>`.
