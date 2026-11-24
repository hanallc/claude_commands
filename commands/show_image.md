---
description: Show an image to the user on the console Images page.
argument-hint: "<image path> <title> | <before path> <after path> <title>"
---

# Show image

Use this when the user must look at an image: before and after shots of a page change, a screen defect, or a chart.
Register the image on the console Images page and give the user the image's own link.
A file path alone is not something the user can view.

An image for the user's review is one dark mode shot at desktop width.
Send a light mode or phone width shot only when the user asks for it.
Check the light mode and phone width yourself when the change needs them.

## Steps

1. Write a short, single-line title that says what the image shows.
   Add one `--look-for` for each point the user needs to check.
   File the before and after shots of a change as one pair.
2. Set `staff_id` to the `id` in your staff row.
   `~/.claude/scripts/lib/py ~/.claude/scripts/production/staff.py get <your worktree path>` prints the row.
3. Register the image or pair.
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py register \
     --before before.png --after after.png --title "Review filter layout" \
     --look-for "The owner filter is below State." --owner "$staff_id" --change sdlc/C12
   ```
   - Use `--image PATH` for a single image.
   - Use `--change` for a change review row. Use `--phase` for phase work.
   - The command accepts `.png`, `.jpg`, `.jpeg`, `.gif` and `.webp` files.
   - It copies the images, so you can delete the source files after it prints `registered`.
4. Give the user the `Image link:` that the command prints for each image or pair. Write it as the full URL in plain text, never as a Markdown link or a wikilink.
   It opens the Images page at that image.
   In a notice to a showrunner, give the same links and the image ids.

The page shows the oldest image first, and a new image goes to the end.
The user answers on an image or pair with **Approved** or **Send Feedback**, and both can carry feedback.
**Send Feedback** means the image is not approved; the first line of its message ends with `your image is not approved. The feedback follows.`
The answer arrives as a message that starts `From the user (via the Images page):`.
Act on it as on the user's words. Then delete the image with the command in the message.
Answer **Ask question** with `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py answer --id <image> --question Q<n> --text "<answer>"`; keep the image for the user.
To remove your own image, run `~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id <id>`.
