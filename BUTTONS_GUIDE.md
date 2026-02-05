# Button & Link Editing Guide

This guide explains how to add buttons and jump links in the homepage (or other pages).

## Where to Edit
- Homepage template: `pc_server/app/templates/index.html`
- Shared button styles: `pc_server/app/static/styles.css`

## Add a Button on the Homepage Hero
1. Open `pc_server/app/templates/index.html`.
2. Find the hero action block:
   ```html
   <div class="hero-cta">
     <a class="btn btn-primary" href="#demos">Explore Demos</a>
   </div>
   ```
3. Add another `<a>` or `<button>` inside the same block:
   ```html
   <a class="btn btn-ghost"
      href="https://example.com"
      target="_blank"
      rel="noopener">My New Link</a>
   ```

## Button Style Options
Use these existing classes:
- `btn btn-primary` (green primary)
- `btn btn-ghost` (transparent with border)
- `btn btn-outline` (subtle outline)
- `btn btn-danger` (danger style)

If you need a new style:
1. Add a class in `pc_server/app/static/styles.css`
2. Reference it on the button in HTML

Example:
```css
.btn-accent {
  background: #ffd25a;
  color: #1b1b1b;
  border-color: rgba(255, 210, 90, 0.6);
}
```
```html
<a class="btn btn-accent" href="https://example.com">My Accent Link</a>
```

## Add Buttons on Other Pages
The demo detail page is `pc_server/app/templates/demo_detail.html`.
- Use the same `btn` classes there.
- Keep spacing consistent by placing buttons inside a `.form-actions` or `.hero-cta` container.

## External Link Best Practice
For external links, always add:
```html
target="_blank" rel="noopener"
```
This opens a new tab and prevents security issues.
