# Butleroy glyph set

24 named SVGs. 24-unit grid, 2 px stroke, round caps and joins, `currentColor`,
no fill.

```xml
<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img"
     aria-label="Bell"><!-- paths --></svg>
```

Two rules, and both exist because of the other one:

- **Stroked, not filled.** A filled glyph at 16 px in a 32 px list row is a solid
  blob; a stroked one keeps its holes and still reads.
- **Named by meaning, not by shape.** `bell.svg`, not `dome.svg`. A set named after
  its geometry cannot be searched by what it is *for*, and the day someone wants a
  settings row for voices they will not think "dome".

Every glyph carries `role="img"` and `aria-label`, because an icon-only button with
no accessible name is unusable with a screen reader — and the overlay already has a
list of things it should not do.

| Glyph | Label | For |
|---|---|---|
| `bell` | Bell | Butleroy itself; the app mark at list size |
| `chat` | Chat | the prompt window |
| `models` | Models | main + deep model, providers |
| `voices` | Voices | TTS voice library |
| `microphone` | Microphone | STT / push-to-talk |
| `speaker` | Speaker | speak-now, audio output |
| `settings` | Settings | advanced configuration |
| `shield` | Authority | the tier ladder, policy, confirmations |
| `tier` | Tiers | T0–T3 cost indicator |
| `workspaces` | Workspaces | approved roots, manifests |
| `vault` | Vault | the Obsidian bridge |
| `audit` | Audit | the hash chain, the record |
| `delegate` | Delegates | tracked workers |
| `teach` | Teaching | the lesson ledger |
| `reflect` | Reflection | the friction ledger |
| `mail` | Mail | IMAP |
| `telegram` | Telegram | the bridge |
| `vram` | VRAM | leases, arbiter, the ceiling |
| `power` | Power | session attended / unattended |
| `key` | Shortcut | chord and hotkeys |
| `plus` / `remove` | Add / Remove | list editing |
| `edit` | Edit | replace |
| `import` / `export` | Import / Export | voice files, backups, transcripts |
| `search` | Search | find in a list |
| `close` | Close | dismiss |

`import` and `export` are drawn as down-into-a-tray and up-out-of-it rather than as
arrows through a wall, because in this product import always means *a file becomes
something the assistant holds* and export always means the reverse.

Not designed yet, deliberately: file-type icons, a spinner, and any state icon.
Add them when something needs them rather than drawing a set nobody has asked for.