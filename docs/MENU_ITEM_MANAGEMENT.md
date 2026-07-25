# Menu Item Management

The admin **Menu Manager** controls the user-facing main menu without source
code edits or a bot restart.

Open **Admin Panel → Menu Manager**, select a menu item, and use its detail
screen to apply changes immediately:

- Show or hide the item
- Enable or disable its action
- Rename the label
- Change its emoji
- Cycle its button color
- Move it up or down
- Restrict it to admins, regular users, or premium users

Changes are stored in the existing `bot_config` table. Menu callbacks and
feature handlers are not changed. A disabled item remains visible but shows a
safe “currently disabled” notice instead of entering its business flow.

“Premium Only” uses the bot’s existing active subscription records
(`subscriptions.status = active` and a future `expires_at`) and does not
introduce a second membership system.

## Custom Button Manager

From **Admin Panel → Menu Manager → Custom Button Manager**, admins can:

- Add, edit, and delete unlimited custom buttons
- Set a name, emoji, color, callback or URL
- Set the 1-based position
- Show or hide each button
- Move buttons up or down directly from the list

Custom buttons are stored in the existing `main_menu_custom_buttons`
configuration value. Existing JSON entries using `label`, `style`, and
`emoji_id` remain compatible; newly managed entries use the clearer `color`
and `emoji` fields. A button uses either a callback or a URL, never both.