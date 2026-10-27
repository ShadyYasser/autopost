# Turning comments off (brand option `comments: off`)

| Platform | What the bot does | Notes |
|---|---|---|
| Instagram | After publishing: `POST /{ig-media-id}` with `comment_enabled=false`. | Needs the permission **`instagram_manage_comments`** on the Page token. If it is missing, the post stays up and you get a Telegram alert telling you to add it. |
| Facebook (Page videos / Reels) | Nothing automatic. | The Graph API has no documented field or endpoint to turn commenting off on a Page video or Reel. See the options below. |
| TikTok (via Buffer) | Nothing automatic. | Buffer's API has no comment setting for TikTok posts (`TikTokPostMetadataInput` only has `title` and `isAiGenerated`). |
| YouTube | Nothing automatic. | Set the default in YouTube Studio → Settings → Upload defaults → Advanced settings → Comments: Disable. Kalima's long videos are uploaded by hand anyway. |

## Facebook options

1. **Page setting (recommended, no code).** Meta Business Suite / Page settings →
   *Privacy* → *Public posts* → limit who can comment. Then use *Moderation* / *Hidden
   words* to auto-hide comments. This is account-wide and covers every post, including
   the ones the bot makes.
2. **Turn off commenting per post by hand.** In the Facebook app, open the Reel/video →
   ⋯ → *Turn off commenting*. Fine for a few posts, not for 3–4 posts a day.
3. **Auto-hide new comments from the bot.** On each run, list the comments on Kalima's
   recent posts and hide them (`POST /{comment-id}` with `is_hidden=true`). This needs
   `pages_read_user_content` + `pages_manage_engagement` on the token. Comments stay
   visible to their authors and are hidden from everyone else, between runs only (up to
   about 10 minutes). It can be added if you want it.

## TikTok option

In the TikTok app for the Kalima Sawaa account: *Settings and privacy* → *Privacy* →
*Comments* → *Who can comment on your videos: No one*. This is account-wide and applies to
posts made through Buffer too.
