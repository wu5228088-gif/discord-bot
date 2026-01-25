import os
import json

import discord
from discord.ext import commands,tasks

import datetime
from datetime import datetime

import requests

import matplotlib.pyplot as plt
import io

import matplotlib.ticker as ticker
import matplotlib.font_manager as fm

token=os.getenv("DISCORD_TOKEN")

data_dir = "/app/data" if os.getenv("ZEABUR") else "."
idfile = os.path.join(data_dir, "idfile.json")
top100file= os.path.join(data_dir, "event_snapshots.json")

intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print ("Good morning afternoon evening!")
    guild_id = 1376730593768771674
    guild = discord.Object(id=guild_id)

lineurl="https://api.hisekai.org/event/live/border"
rankurl="https://api.hisekai.org/event/live/top100"

def loadsjson(path):
    if os.path.exists(path):
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    return{}

def savesjson(path,data):
    with open(path,"w",encoding="utf-8") as f:
        json.dump(data,f,indent=4,ensure_ascii=False)

#全域同步
@bot.event
async def on_ready():
    if not top100track.is_running():
        top100track.start()
    try:
        synced = await bot.tree.sync()
        print(f"成功全域同步了 {len(synced)} 個指令")
    except Exception as e:
        print(f"同步指令時發生錯誤: {e}")

#每10分鐘抓一次api
@tasks.loop(minutes=10)
async def top100track():
    try:
        resp=requests.get(rankurl,timeout=10)
        resp.raise_for_status()
        topdata=resp.json()

        currentid=topdata.get("id") or topdata.get("event_id")
        rankdata=topdata.get("top_100_player_rankings", [])

        storage=loadsjson(top100file)
        if not storage or storage.get("last_id") !=currentid:
            storage={"last_id":currentid,"snapshots": []}
        
        timestamp=datetime.now().strftime("%m/%d %H:%M")
        scoresmap={}
        for item in rankdata:
            p_name=item.get("name","Unknown")

            p_id=str(item.get("last_player_info",{}).get("profile",{}).get("id"))

            scoresmap[p_id] = {
                "score": item.get("score"),
                "name": p_name
            }

        if not storage["snapshots"] or scoresmap != storage["snapshots"][-1]["data"]:
            storage["snapshots"].append({"time": timestamp, "data": scoresmap})
            if len(storage["snapshots"]) > 1500: storage["snapshots"].pop(0)
            savesjson(top100file, storage)
        
        if len(storage["snapshots"]) > 1500: storage["snapshots"].pop(0)
            
        savesjson(top100file, storage)

    except Exception as e:
        print(f"api獲取失敗:{e}")

#精彩片段
@bot.hybrid_command()
async def line(ctx):
    try:
        await ctx.defer()
    except Exception as e:
        print(f"Defer 失敗 (可能是重複觸發): {e}")
    

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
        
idfile="idfile.json"

useridlist=loadsjson(idfile)

#綁定id
@bot.hybrid_command()
async def bind(ctx,gameid:str,notify:bool=True):
  
    useridlist[str(ctx.author.id)]={"game_id":gameid,"notify":notify}
    savesjson(idfile,useridlist)
    await ctx.send(f"遊戲id綁定成功! id:{gameid}")

#繪圖
@bot.hybrid_command()
async def graph(ctx):
    await ctx.defer()
    useridlist=loadsjson(idfile)
    userinfo=useridlist.get(str(ctx.author.id))

    if not userinfo:
        await ctx.interaction.followup.send("請先綁定id")
        return
    if isinstance(userinfo, dict):
        game_id=userinfo.get("game_id")
        player_name=userinfo.get("name", ctx.author.display_name)
    else:
        game_id=userinfo
        player_name=ctx.author.display_name

    storage = loadsjson(top100file)

    times,scores=[],[]
    last_game_name=None
    current_year = datetime.now().year
    for snap in storage.get("snapshots",[]):
        player_entry=snap["data"].get(str(game_id))
        if player_entry is not None:
            if isinstance(player_entry,dict):
                s=player_entry.get("score")
                last_game_name=player_entry.get("name")
            else:
                s=player_entry
            dt=datetime.strptime(f"{current_year}/{snap['time']}", "%Y/%m/%d %H:%M")
            times.append(dt)
            scores.append(s)

    if len(scores)<2:
        await ctx.interaction.followup.send("數據不足或你被肘出100名了")
        return
    
    font_path="./msjh.ttc"
    if os.path.exists(font_path):
        fe=fm.FontEntry(fname=font_path,name='CustomFont')
        fm.fontManager.ttflist.insert(0,fe)
        plt.rcParams['font.family']=fe.name
    else:
        plt.rcParams['font.sans-serif']=['Microsoft JhengHei', 'SimHei', 'sans-serif']

    plt.rcParams['axes.unicode_minus']=False
   
    
    
    plt.figure(figsize=(10, 6))
    plt.rcParams['axes.facecolor'] = 'white' 
    plt.plot(times, scores, color='#1ABC9C',linewidth=3, markersize=4)

    current_score = scores[-1]
    if current_score > 0:
        tick_spacing = current_score * 0.125 
        plt.gca().yaxis.set_major_locator(ticker.MultipleLocator(tick_spacing))

    import matplotlib.dates as mdates
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))

    start_time=times[0]
    now_time=datetime.now()
    plt.xlim(start_time,now_time)

    plt.ylim(0,current_score*1.1)

    plt.gca().yaxis.set_major_formatter(ticker.FuncFormatter(lambda x,p:format(int(x),',')))

    displaytitle=last_game_name if last_game_name else player_name

    plt.xticks(rotation=45)
    plt.title(f"{displaytitle}",color='black',fontsize=14,fontweight='bold')
    plt.tick_params(colors='black',which='both') 

    plt.grid(False)
    
    for spine in plt.gca().spines.values():
        spine.set_color('black')

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', facecolor='white', edgecolor='none')
    buf.seek(0)
    plt.close()

    await ctx.interaction.followup.send(file=discord.File(buf, "trend.png"))



     
#特定id追蹤
@bot.hybrid_command()
async def playerrank(ctx):
    try:
        await ctx.defer()
    except Exception as e:
        print(f"Defer 失敗 (可能是重複觸發): {e}")


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
            p_id=item.get("last_player_info",{}).get("profile",{}).get("id")
            if p_id==str(gameid):
                playerinfo=item
                playerinfo["rank_num"] =index+1
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

            await ctx.interaction.followup.send(embed=embed)
        else:
            await ctx.interaction.followup.send("你不在100名內")
    except Exception as e:
        await ctx.interaction.followup.send(f"錯誤:{e}")
        
bot.run(token)


