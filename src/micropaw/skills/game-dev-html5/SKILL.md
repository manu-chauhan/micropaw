---
name: game-dev-html5
description: Build and test a small single-file HTML5 canvas game in small steps with preview, screenshots and console checks.
---

# Single-file HTML5 game

Work in the workspace. One file, `game.html`, with inline CSS and JavaScript and no downloads.

## Loop
1. Write the smallest playable version first: one canvas, a game loop with requestAnimationFrame, keyboard input, one thing the player can do.
2. Add a text state hook so you can read the game without looking at it:
   `window.__state = () => ({score, player: {x, y}, over})` or a plain object kept up to date.
3. Run `web_browse` with action `preview` and the file path. It serves the file from a local port and returns a screenshot, console errors and the value of `window.__state`.
4. Fix the first console error before anything else. Then check the screenshot: is the picture what you meant?
5. Use `type` and `click` actions to press keys and buttons, then preview state again to see that things moved.
6. Add one feature at a time. Preview after each one.

## Rules
- Fixed time step for movement (use the time since the last frame), not frame counts.
- Keep all game state in one object so the hook is easy to write.
- Screenshots matter for canvas games: a page can load with no errors and still draw nothing.
- If the model cannot see images, rely on `window.__state` and console errors, and call preview with screenshot false.
- The previewed page cannot reach the network. Draw shapes in code instead of loading images.
- For bigger engines (Godot, Unity, Blender) use an MCP server, not this skill.
