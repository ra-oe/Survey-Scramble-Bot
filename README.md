# Survey Scramble Bot
A bot for The Jackbox Survey Scramble game.

## What can it do?
It can play with other people, or other bots, in Survey Scramble games. You can spawn as many bots as your pc can handle, or the limit of Jackbox games (8-10 players, 10000 audience)

Here are the gamemodes it can play and how well it plays it:
| Gamemode | Can play? | Playabilty |
|----------|-----------|------------|
| HiLo | Yes | Great (it was originally made for just this) |
| Speed | Yes | Works, but slow |
| Squares | Yes | Good |
| Bounce | No | Absolute dogshit |
| Dares | Yes | Good, but sometimes gives up |
| Dash | Yes | Good |

## How the fuck does it join the game???
Easy, it just connects to the Ecast servers...
just kidding, its not *THAT* easy.

Jackbox's Ecast uses WebSockets, but when you try to connect normally, you can't, because it needs a specific subprotocol `ecast-v0` to even get accepted to connect.

Heres an example of a valid Ecast WebSocket url:

`wss://ecast-prod-use2.jackboxgames.com/api/v2/rooms/MVJY/play?role=player&name=AIBot_1&format=json&user-id=f38f6b18-86f6-19ff-eb50-190559d11235`

**When the bot joins, the server will send something like this:**

```json
{
  "opcode": "client/welcome",
  "result": {
    "profile": {
      "id": 3,
      "roles": {
        "player": {
          "name": "AIBot"
        }
      }
    }
  }
}
```
**When the server sends a question/prompt it will send something like this:**
```json
{
  "opcode": "object",
  "result": {
    "key": "player:3",
    "val": {
      "textEntry": {
        "prompt": "Name a color in a crayon box.",
        "responseKey": "textGuess:3"
      }
    }
  }
}
```
**When the bot sends a response:**
```json
{
  "seq": 1,
  "opcode": "text/update",
  "params": {
    "key": "textGuess:3",
    "val": "blue"
  }
}
```
Or voting on a question with multiple choice:
```json
{
  "seq": 2,
  "opcode": "object/update",
  "params": {
    "key": "voteResponse:3",
    "val": {
      "index": 1
    }
  }
}
```
## How does it know if the room even exists?
Magic....

...no but really, it uses the API url for the code. Example: `https://ecast.jackboxgames.com/api/v2/rooms/DSDM`

The API response looks like this:
```json
{
  "ok": true,
  "body": {
    "appId": "928c228b-4d28-44a1-aee0-88678fe27b15",
    "appTag": "bigsurvey",
    "audienceEnabled": true,
    "code": "DSDM",
    "host": "ecast-prod-use2.jackboxgames.com",
    "audienceHost": "ecast-prod-use2.jackboxgames.com",
    "locked": false,
    "full": false,
    "maxPlayers": 10,
    "minPlayers": 2,
    "moderationEnabled": false,
    "passwordRequired": false,
    "twitchLocked": false,
    "locale": "en",
    "keepalive": true,
    "controllerBranch": ""
  }
}
```
| Key | What is it/what does it do? |
|-----|-------------|
|`appId`|The ID for the host's app|
|`appTag`|The Jackbox game being hosted|
|`audienceEnabled`|If audience can join the game|
|`code`|Room code|
|`host`|The Jackbox Ecast url to connect to the game|
|`audienceHost`|The Jackbox Ecast url for audience|
|`locked`|If the game is currently in progress|
|`full`|If all the player spots are taken|
|`maxPlayers`|The amount of players that can join|
|`minPlayers`|The amount of players to start the game|
|`moderationEnabled`|If moderators can join at [mod.jackbox.tv](https://mod.jackbox.tv/) and moderate player inputs|
|`passwordRequired`|If a password is required to join the game|
|`twitchLocked`|If the room is only joinable if youre logged into Twitch|
|`locale`|Game language|
|`keepalive`|If the room will stay open even if inactive|
|`controllerBranch`|Used by Jackbox developers to push beta/hotfix versions of [jackbox.tv](https://jackbox.tv/) to players for specific rooms during testing|

## How does it answer questions?
It uses really any AI you want, and guesses from the prompt and other crap. A small thing, though, is that it can use a **LOT** of resources on your pc if you spawn a ton of them, because each bot runs on its own independent  thread.

Supports OpenRouter, DeepSeek, and Ollama. Support for more services coming sometime soon.

Had to build a bunch of custom filters and memory shit so the AI doesn't break the game:
* AI models love to write long shit or use spaces (like "ice cream"). The code forces the AI to output exactly one word, strips all spaces and special characters, and mushes them together (like `icecream` or `rollercoaster`) with a 24 character cap.
* If a bot guesses a word and the Jackbox server rejects it (or if it already guessed it), the bot remembers that failed word and explicitly tells the AI on the next turn: *"Do not guess this again, you fuckass retard piece of shit ass clanker."* This stops them from getting stuck in infinite dumbtard loops.
* The bots actually read if the game wants a "High" popularity or "Low" popularity answer and tells the AI to target super obvious words or obscure hipster garbage depending on the score. In Dares, it even knows what reference word it's comparing against... most of the time.
## Can it play other Jackbox games?
No, but it *can* **join** them and waste a player space (or audience) so thats fun and cool (especially joining livestreams that dont have `twitchLocked` enabled!!!11!)

## This code is AI *SHIT* so *I FUCKING HATE IT!!!!*
Yeah no fucking shit sherlock I'm ass at python, so I had to vibe code it.
**But dont worry I did all the other crap myself because im not a retard that relies on ai for everything.**
