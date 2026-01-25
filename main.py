import os
import json

import discord
from discord.ext import commands

import datetime
from datetime import datetime

import requests

token=os.getenv("DISCORD_TOKEN") or "MTQ2MzUxNjg2MjIyNTg0MjIyOA.G0JegA.v1a-MW_U7JX3XL2OaKoFdAvBFuV2PqLgZlxU34"

intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)
@bot.command(name="123")
async def asdf(ctx):
    await ctx.send("機鳴哨是甲")
@bot.event
async def on_ready():
    print ("Good morning afternoon evening!")
    guild_id = 1376730593768771674
    guild = discord.Object(id=guild_id)

lineurl="https://api.hisekai.org/event/live/border"
rankurl="https://api.hisekai.org/event/live/top100"

#全域同步
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✨ 成功全域同步了 {len(synced)} 個指令！")
    except Exception as e:
        print(f"同步指令時發生錯誤: {e}")

#精彩片段
@bot.hybrid_command()
async def line(ctx):
    await ctx.defer()
    try:
        respline=requests.get(lineurl)
        respline.raise_for_status()
        linejson=respline.json()
        linelist=linejson.get("border_player_rankings",[])
        resptop100=requests.get(rankurl)
        resptop100.raise_for_status()
        top100json=resptop100.json()
        top100list=top100json.get("top_100_player_rankings", [])
       
        if not linelist:
            await ctx.interaction.followup.send("api網站爆了")
            return
        
        lines=[]
        target=[10,20,30,40,50]

        for tr in target:
            match=next((item for item in top100list if item.get("rank") == tr), None)
            if match:
                s=match.get("score",0)
                lines.append(f"**{tr}名**:`{s:,}`")

        for item in linelist:
            rank=item.get("rank","?")
            score=item.get("score",0)
            lines.append(f"**{rank}名**:`{score:,}`")

        ranktext="\n".join(lines)
        eventid=linejson.get("id","?")
        eventname=linejson.get("name","當期活動")

        embed=discord.Embed(
            title=f"{eventid}-{eventname}",
            description=ranktext,
            color=0x1ABC9C
        )
        updatetime=datetime.now().strftime('%Y-%m-%d %H:%M')
        embed.set_footer(text=f"最後更新於:{updatetime}")
        await ctx.interaction.followup.send(embed=embed)
    except Exception as e:
        await ctx.interaction.followup.send(f"錯誤:{e}")

#特定id追蹤        
idfile="idfile.json"

def loads():
    if os.path.exists(idfile):
        with open(idfile,"r",encoding="utf-8") as f:
            return json.load(f)
    return{}

def saves(data):
    with open(idfile,"w",encoding="utf-8") as f:
        json.dump(data,f,indent=4)

useridlist=loads()

@bot.hybrid_command()
async def bind(ctx,gameid:str):
    useridlist[str(ctx.author.id)]=gameid
    saves(useridlist)
    await ctx.send(f"遊戲id綁定成功! id:{gameid}")


     

@bot.hybrid_command()
async def playerrank(ctx):
    gameid=useridlist.get(str(ctx.author.id))
    if not gameid:
        await ctx.send("請先綁定id")
        return
    try:
        resp=requests.get(rankurl)
        resp.raise_for_status()
        topdata=resp.json()
        rankdata=topdata.get("top_100_player_rankings", [])
        playerinfo=None
        for index,item in enumerate(rankdata):
            profile=item.get("last_player_info", {}).get("profile", {})
            if str(profile.get("id")) == gameid:
                playerinfo=item
                playerinfo["rank_num"]=index+1
                break
        if playerinfo:
            name=playerinfo.get("name","未知")
            tscore=playerinfo.get("score",0)
            rank = playerinfo["rank_num"]
            stat1h=playerinfo.get("last_1h_stats", {})
            count=stat1h.get("count",0)
            score=stat1h.get("score",0)
            speed=stat1h.get("speed",0)
            lastscore=stat1h.get("lastscore",0)
            avg=stat1h.get("average",0)

            embed=discord.Embed(
                title=f"{name}",
                color=0x00ff00
            )
            embed.description=f"排名:{rank}\n總分:{tscore:,}"
            embed.add_field(name="時速",value=f"{score:,}",inline=False)
            embed.add_field(name="周回",value=f"{count}",inline=False)
            embed.add_field(name="場均",value=f"{avg}",inline=False)

            await ctx.send(embed=embed)
        else:
            await ctx.send("你不在100名內")
    except Exception as e:
        await ctx.send(f"錯誤:{e}")

bot.run(token)

