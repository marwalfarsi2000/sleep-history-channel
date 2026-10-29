import React from "react";
import {AbsoluteFill, Composition, registerRoot, useCurrentFrame, useVideoConfig} from "remotion";
import sample from "../data/sample.json";
import style from "../data/style.json";

type Scene = "library" | "courtyard" | "harbor";
type Beat = {startSeconds:number; kind:Scene};
type Props = {episodeId:string;language:"ar";title:string;subtitle:string;durationSeconds:number;scenes:Beat[]};
const defaults = sample as Props;
const clamp = (x:number) => Math.max(0,Math.min(1,x));
const backgrounds:Record<Scene,string> = {
 library:"radial-gradient(ellipse at 70% 60%,#382f2c,transparent 43%),linear-gradient(145deg,#17263c,#08111c)",
 courtyard:"radial-gradient(ellipse at 65% 56%,#293349,transparent 43%),linear-gradient(145deg,#172338,#08111c)",
 harbor:"radial-gradient(ellipse at 70% 67%,#332d30,transparent 41%),linear-gradient(150deg,#142139,#08111c)",
};
const validate = (p:Props) => {
 if(p.language!=="ar" || !p.title || !p.subtitle || !/^[a-z0-9_-]+$/.test(p.episodeId)) throw Error("Invalid Arabic episode props");
 if(!Number.isInteger(p.durationSeconds) || p.durationSeconds<15 || p.durationSeconds>300) throw Error("V1 renders short clips only");
 if(p.scenes.length<1 || p.scenes.length>6 || p.scenes[0].startSeconds!==0) throw Error("Invalid storyboard");
 let prev=-1;
 for(const b of p.scenes) {
  if(!Number.isInteger(b.startSeconds) || b.startSeconds<=prev || b.startSeconds>=p.durationSeconds || !Object.hasOwn(backgrounds,b.kind)) throw Error("Invalid storyboard beat");
  prev=b.startSeconds;
 }
};
validate(defaults);

/** Generic silhouettes, not purported historic architectural reconstructions. */
const Motif:React.FC<{kind:Scene}> = ({kind}) => <svg viewBox="0 0 1920 1080" width="100%" height="100%" aria-hidden="true" style={{position:"absolute",inset:0,opacity:0.45}}>
 <g fill="#06101b">
 {kind==="library" && <><rect x="0" y="800" width="1920" height="280"/><rect x="140" y="200" width="160" height="600"/><rect x="350" y="250" width="170" height="550"/><rect x="140" y="440" width="430" height="24"/><rect x="140" y="680" width="430" height="24"/></>}
 {kind==="courtyard" && <><rect y="790" width="1920" height="290"/><path d="M90 790 V230 H480 V790 H390 V510 A105 105 0 0 0 180 510 V790 Z"/><path d="M800 790 V260 H1250 V790 H1150 V530 A120 120 0 0 0 910 530 V790 Z"/></>}
 {kind==="harbor" && <><path d="M0 810 Q450 788 960 812 T1920 810 V1080 H0 Z"/><rect x="170" y="575" width="230" height="250"/><rect x="430" y="660" width="150" height="165"/><rect x="1540" y="665" width="160" height="160"/></>}
 </g>
 </svg>;

const CalmHistorySleeper:React.FC<Props> = (p) => {
 validate(p);
 const frame=useCurrentFrame(), {fps}=useVideoConfig(), fade=style.fadeSeconds*fps;
 const dx=Math.sin(frame/(fps*42))*style.maxDriftPx,dy=Math.cos(frame/(fps*65))*4;
 const intro=clamp(frame/(fps*5))*clamp((fps*25-frame)/(fps*7));
 return <AbsoluteFill style={{backgroundColor:style.palette.night,overflow:"hidden"}}>
 {p.scenes.map((b,i)=>{
  const from=b.startSeconds*fps, next=p.scenes[i+1]?.startSeconds;
  const incoming=i===0?1:clamp((frame-from+fade)/fade);
  const outgoing=next===undefined?1:1-clamp((frame-next*fps+fade)/fade);
  return <AbsoluteFill key={from} style={{background:backgrounds[b.kind],opacity:incoming*outgoing}}>
   <AbsoluteFill style={{transform:"translate("+dx+"px,"+dy+"px) scale(1.015)"}}><Motif kind={b.kind}/></AbsoluteFill>
   <div aria-hidden="true" style={{position:"absolute",right:"16%",top:"20%",width:150,height:150,borderRadius:"50%",background:style.palette.mutedGold,opacity:0.19,filter:"blur(58px)"}}/>
  </AbsoluteFill>;
 })}
 <AbsoluteFill style={{background:"linear-gradient(to bottom,transparent,rgba(8,17,28,0.26))"}}/>
 <AbsoluteFill style={{direction:"rtl",alignItems:"center",justifyContent:"center",padding:"0 180px",textAlign:"center",opacity:intro}}>
  <div dir="rtl" lang="ar" style={{maxWidth:1200,fontFamily:"'Noto Naskh Arabic', Amiri, Tahoma, Arial, serif"}}>
   <div style={{fontSize:70,lineHeight:1.6,color:style.palette.ivory,textShadow:"0 2px 18px rgba(0,0,0,.5)"}}>{p.title}</div>
   <div style={{fontSize:30,lineHeight:1.7,color:style.palette.mutedGold}}>{p.subtitle}</div>
  </div>
 </AbsoluteFill>
 </AbsoluteFill>;
};
const Root:React.FC = () => <Composition id="CalmHistorySleeper" component={CalmHistorySleeper}
 width={1920} height={1080} fps={24} durationInFrames={24*defaults.durationSeconds}
 defaultProps={defaults} calculateMetadata={({props})=>{validate(props);return {durationInFrames:24*props.durationSeconds};}}/>;
registerRoot(Root);
