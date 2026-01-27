import os
import json

import discord
from discord.ext import commands,tasks

import datetime
from datetime import datetime,timedelta,timezone

import requests

import matplotlib.pyplot as plt
import io

import matplotlib.ticker as ticker
import matplotlib.font_manager as fm

token=os.getenv("DISCORD_TOKEN")

TW=timezone(timedelta(hours=8))

data_dir = "/app/data" if os.getenv("ZEABUR") else "."
idfile = os.path.join(data_dir, "idfile.json")
top100file= os.path.join(data_dir, "event_snapshots.json")

intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix="!", intents=intents)


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

def get_event_start_time():
    try:
        resp = requests.get(rankurl, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        start_str = data.get("start_at")
        if not start_str:
            return None

        # 解析 UTC → 轉台灣時間
        dt_utc = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        return dt_utc.astimezone(TW)

    except Exception as e:
        print("活動開始時間取得失敗:", e)
        return None

#全域同步
@bot.event
async def on_ready():
    if not top100track.is_running():
        top100track.start()
    guildid=1224368638442733739
    guild=discord.Object(id=guildid)
    try:
        await bot.tree.sync(guild=guild)
        synced=await bot.tree.sync()
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
        
        timestamp=datetime.now(TW).strftime("%m/%d %H:%M")
        scoresmap={}
        for item in rankdata:
            p_name=item.get("name","Unknown")

            p_id=str(item.get("last_player_info",{}).get("profile",{}).get("id"))

            scoresmap[p_id] = {
                "score": item.get("score"),
                "name": p_name,
                "rank": item.get("rank")
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
        updatetime=datetime.now(TW).strftime('%Y-%m-%d %H:%M')
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
    nowtw=datetime.now(TW)
    current_year=nowtw.year
    for snap in storage.get("snapshots",[]):
        player_entry=snap["data"].get(str(game_id))

        if player_entry is not None:
            if isinstance(player_entry,dict):
                s=player_entry.get("score")
                last_game_name=player_entry.get("name")
            else:
                s=player_entry
            
            try:
                dt_str=(f"{current_year}/{snap['time']}")
                dt=datetime.strptime(dt_str,"%Y/%m/%d %H:%M")
                dt=dt.replace(tzinfo=TW)
                times.append(dt)
                scores.append(s)
            except Exception as e:
                print(f"解析失敗的時間字串是: {snap['time']}, 錯誤: {e}")

    if len(scores)<2:
        await ctx.interaction.followup.send("數據不足或你被肘出100名了")
        return
    
    combined=sorted(zip(times, scores))
    times,scores=zip(*combined)
    times,scores=list(times), list(scores)
    
    font_path="./NotoSansTC-Bold.ttf"
    if os.path.exists(font_path):
        fe=fm.FontEntry(fname=font_path,name='NotoSansTC-Bold')
        fm.fontManager.ttflist.insert(0,fe)
        plt.rcParams['font.family']=[fe.name]
    else:
        plt.rcParams['font.sans-serif']=['Microsoft JhengHei','Noto Sans TC','sans-serif']

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

    event_start=get_event_start_time()
    now_time=datetime.now(TW)
 

    if event_start:
        plt.xlim(event_start, now_time)
    else:
        plt.xlim(min(times), now_time)

    plt.ylim(0,max(current_score*1.1,100))

    plt.gca().yaxis.set_major_formatter(ticker.FuncFormatter(lambda x,p:format(int(x),',')))

    displaytitle=last_game_name if last_game_name else player_name


    plt.xticks(rotation=45,fontsize=10,weight=1000)
    plt.yticks(fontsize=10,weight=1000)
    plt.title(f"{displaytitle}",color='black',fontsize=20,weight=1000,pad=15)
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
        await ctx.interaction.followup.send("請先綁定id")
        return
    if isinstance(gameid, dict):
        target_game_id = gameid.get("game_id")
    else:
        target_game_id = gameid

    try:
        resp=requests.get(rankurl)
        resp.raise_for_status()
        topdata=resp.json()
        rankdata=topdata.get("top_100_player_rankings", [])
        playerinfo=None
        p_index=-1

        for index,item in enumerate(rankdata):
            p_id=item.get("last_player_info",{}).get("profile",{}).get("id")
            if str(p_id)==str(target_game_id):
                playerinfo=item
                playerinfo["rank_num"]=item.get("rank") or (index + 1)
                p_index=index
                break

        if playerinfo:
            name=playerinfo.get("name","未知")
            tscore=playerinfo.get("score",0)
            rank=playerinfo["rank_num"]
            stat1h=playerinfo.get("last_1h_stats", {})
            hscore=stat1h.get("score",0)
            count=stat1h.get("count",0)
            lastscore=playerinfo.get("last_score",0)
            avg=stat1h.get("average",0)

            diff_text = ""
            
            if p_index > 0:
                prev_player = rankdata[p_index - 1]
                prev_score = prev_player.get("score", 0)
                diff_up = prev_score - tscore
                diff_text += f"與前一名 ({prev_player.get('rank')}名) 差距: `-{diff_up:,}`\n"
            
            if p_index < len(rankdata) - 1:
                next_player = rankdata[p_index + 1]
                next_score = next_player.get("score", 0)
                diff_down = tscore - next_score
                diff_text += f"與後一名 ({next_player.get('rank')}名) 差距: `+{diff_down:,}`"

            embed=discord.Embed(
                title=f"{name}",
                color=0x00ff00
            )
            embed.description=f"排名:{rank}\n總分:{tscore:,}\n\n{diff_text}"
            embed.add_field(name="時速",value=f"{hscore:,}",inline=False)
            embed.add_field(name="周回",value=f"{count}",inline=False)
            embed.add_field(name="場均",value=f"{avg}",inline=False)
            embed.add_field(name="最近一把pt",value=f"{lastscore}",inline=False)
            updatetime=datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')
            embed.set_footer(text=f"最後更新於: {updatetime}")

            await ctx.interaction.followup.send(embed=embed)
        else:
            await ctx.interaction.followup.send("你不在100名內")
    except Exception as e:
        await ctx.interaction.followup.send(f"錯誤:{e}")

#特定排名追蹤
@bot.hybrid_command()
async def trackrank(ctx,input:int):
    try:
        await ctx.defer()
    except Exception as e:
        print(f"Defer 失敗: {e}")

    if not(1<=input<=100):
        await ctx.interaction.followup.send("請輸入1~100之間的整數")
        return
    try:
        resp=requests.get(rankurl)
        resp.raise_for_status()
        topdata=resp.json()

        rankdata=topdata.get("rankings") or topdata.get("top_100_player_rankings", [])
        p_index=input-1

        if p_index<len(rankdata):
            playerinfo=rankdata[p_index]
        
            name=playerinfo.get("name","未知")
            tscore=playerinfo.get("score",0)
            stat1h=playerinfo.get("last_1h_stats", {})
            hscore=stat1h.get("score",0)
            count=stat1h.get("count",0)
            lastscore=playerinfo.get("last_score",0)
            avg=stat1h.get("average",0)

            diff_text = ""
            
            if p_index > 0:
                prev_player = rankdata[p_index - 1]
                prev_score = prev_player.get("score", 0)
                diff_up = prev_score - tscore
                diff_text += f"與前一名 ({prev_player.get('rank')}名) 差距: `-{diff_up:,}`\n"
            
            if p_index < len(rankdata) - 1:
                next_player = rankdata[p_index + 1]
                next_score = next_player.get("score", 0)
                diff_down = tscore - next_score
                diff_text += f"與後一名 ({next_player.get('rank')}名) 差距: `+{diff_down:,}`"

            embed=discord.Embed(
                    title=f"{input}名-{name}",
                    color=0x00ff00
                )
            embed.description=f"排名:{input}\n總分:{tscore:,}\n\n{diff_text}"
            embed.add_field(name="時速",value=f"{hscore:,}",inline=False)
            embed.add_field(name="周回",value=f"{count}",inline=False)
            embed.add_field(name="場均",value=f"{avg}",inline=False)
            embed.add_field(name="最近一把pt",value=f"{lastscore}",inline=False)
            updatetime=datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')
            embed.set_footer(text=f"最後更新於: {updatetime}")
            await ctx.interaction.followup.send(embed=embed)
        else:
            await ctx.interaction.followup.send("錯誤")
    except Exception as e:
        await ctx.interaction.followup.send(f"錯誤:{e}")

#特定排名繪圖
@bot.hybrid_command()
async def trackgraph(ctx,input:int):
    try:
        await ctx.defer()
    except Exception as e:
        print(f"Defer 失敗: {e}")

    if not(1<=input<=100):
        await ctx.interaction.followup.send("請輸入1~100之間的整數")
        return
    storage=loadsjson(top100file)
    
    times,scores=[], []
    last_game_name=None
    nowtw=datetime.now(TW)
    current_year=nowtw.year
    for snap in storage.get("snapshots",[]):
        player_entry=None
        for uid,pdata in snap.get("data",{}).items():
            if isinstance(pdata, dict):
                r=pdata.get("rank")
                if r is not None and int(r) == int(input):
                    player_entry = pdata
                    break

        if player_entry is not None:
            s = player_entry.get("score")
            last_game_name = player_entry.get("name") # 記錄最後一個點的名字作為標題
            
            try:
                dt_str=(f"{current_year}/{snap['time']}")
                dt=datetime.strptime(dt_str,"%Y/%m/%d %H:%M")
                dt=dt.replace(tzinfo=TW)
                times.append(dt)
                scores.append(s)
            except Exception as e:
                print(f"解析失敗的時間字串是:{snap['time']},錯誤:{e}")

    if len(scores)<2:
        await ctx.interaction.followup.send("數據不足或你被肘出100名了")
        return
    combined=sorted(zip(times, scores))
    times,scores=zip(*combined)
    times,scores=list(times), list(scores)
    
    font_path="./NotoSansTC-Bold.ttf"
    if os.path.exists(font_path):
        fe=fm.FontEntry(fname=font_path,name='NotoSansTC-Bold')
        fm.fontManager.ttflist.insert(0,fe)
        plt.rcParams['font.family']=[fe.name]
    else:
        plt.rcParams['font.sans-serif']=['Microsoft JhengHei','Noto Sans TC','sans-serif']

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

    event_start=get_event_start_time()
    now_time=datetime.now(TW)
 

    if event_start:
        plt.xlim(event_start, now_time)
    else:
        plt.xlim(min(times), now_time)

    plt.ylim(0,max(current_score*1.1,100))

    plt.gca().yaxis.set_major_formatter(ticker.FuncFormatter(lambda x,p:format(int(x),',')))

    displaytitle=f"{input}名-{last_game_name}" if last_game_name else f"{input}名"


    plt.xticks(rotation=45,fontsize=10,weight=1000)
    plt.yticks(fontsize=10,weight=1000)
    plt.title(f"{displaytitle}",color='black',fontsize=20,weight=1000,pad=15)
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
    
bot.run(token)


