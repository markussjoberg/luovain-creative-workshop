"""
Creative Skills Week – Co-creation with the AI
Single-file Flask app with SQLite for:
  1) Participant on-boarding + AI dialogue + logging
  2) Facilitator dashboard + 3-person group formation from logs
  3) Group co-creation page where AI guides fast concepting (30 min)

Quick start
-----------
1) python3 -m venv .venv && source .venv/bin/activate
2) pip install flask sqlalchemy openai python-dotenv numpy scikit-learn
3) export OPENAI_API_KEY=sk-...
4) python app.py
5) Open participant URL:  http://localhost:8000/
   Open facilitator URL: http://localhost:8000/facilitator

Notes
-----
- Uses OpenAI Chat Completions-like interface via openai>=1.0.0 "client.chat.completions.create".
  If you prefer the Responses API, swap the call in llm_chat() accordingly.
- Stores minimal PII (name). For research use, consider hashing or pseudonymising immediately
  and showing a consent screen. See TODOs at bottom for GDPR.
- Grouping is done via embeddings + greedy triplet packing with topical similarity and
  complementary need/experience signals.
- All prompts are embedded below and can be tweaked live.

"""
from __future__ import annotations
import os
import json
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from flask import Flask, request, jsonify, render_template_string, redirect
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, ForeignKey, func, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship  
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import OperationalError

from dotenv import load_dotenv
import numpy as np

# Optional: sklearn for simple clustering helpers
from sklearn.preprocessing import normalize

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # linting only

load_dotenv()

# Use PostgreSQL for research data persistence and better concurrency
DB_URL = os.environ.get("CSW_DB_URL", "postgresql://gxmasj@localhost/creative_workshop")
OPENAI_MODEL = os.environ.get("CSW_MODEL", "gpt-5")
EMBED_MODEL = os.environ.get("CSW_EMBED_MODEL", "text-embedding-3-small")
SYSTEM_INSTRUCTIONS_PARTICIPANT = """
You are an expert facilitator helping a creative professional reflect on their AI usage
and upskilling needs. Work in short, clear messages, but be kind. Ask one question at a time.
Language: mirror the user's language; if unclear, default to English.

IMPORTANT: Keep the conversation focused and conclude after 6-8 exchanges total.

Before anything else, ask the two standard questions :
"Hi ther! What is your name?"
And then the sedcond
"What kind of creative work do you do (profession or hobby)?"

After collecting both, continue with a brief, focused dialogue (3-5 more exchanges) to map:
- Creative tasks they already use AI for (concrete examples)
- Creative tasks they would like to learn to use AI for in the next 6 months
- Perceived blockers (skills, tools, ethics, IP, organisational)

After 5-7 total exchanges, conclude with: "Thanks [name]! Here's your upskilling profile:" 
followed by a compact bullet summary of their needs, then: "You're ready to connect with others for co-creation! The facilitator will form groups soon."
"""

SYSTEM_INSTRUCTIONS_GROUP_CHAIR = """
You are a creative sparring partner guiding a fast concepting session (fail fast / lean prototyping).
Be encouraging, playful, and inspiring – keep energy high and momentum fast.
Use short, motivating messages, propose concrete drafts, and celebrate progress.
 
Workflow:
 
Start by asking: “Based on your shared interests and possible synergies – what could you imagine creating together?”
Guide the group through:
Ask only one playful question at a time, and always build on their answers.
Keep the vibe collaborative, energetic, and imaginative until the concept feels ready.
 
Ideation – spark many raw, fun ideas
Choosing – help them quickly pick one
Refining – add purpose, audience, key features
Finalising – shape into a clear, exciting concept
"""

SYSTEM_INSTRUCTIONS_SUMMARY_TO_TOPICS = """
You turn multiple participant need-summaries into 5–10 shared themes for co-creation.
Output JSON with keys: themes:[{name, rationale, representative_quotes[]}]. Keep it concise.
"""

SYSTEM_INSTRUCTIONS_GROUPING = """
You are forming 3-person groups for a creative co-creation sprint. Goals:
1. Topical alignment: similar creative interests/domains
2. Skill complementarity: mix of AI experience levels and creative backgrounds
3. Learning synergy: where participants can help each other

Analyze each participant's:
- Creative role/domain
- Current AI usage patterns
- Learning goals and blockers
- Personality indicators from conversation style

Form balanced groups where members can:
- Work on similar creative concepts
- Share different perspectives and skills
- Support each other's learning needs

Return JSON: {"groups":[{"name":"Group 1", "participants":["name1","name2","name3"], "rationale":"Brief explanation of why this combination works"}]}
"""

# --- Flask setup
app = Flask(__name__)

# Global session state for workshop session management
CURRENT_SESSION_ID = None

engine = create_engine(
    DB_URL, 
    echo=False, 
    future=True,
    pool_pre_ping=True,
    pool_size=20,  # Support many concurrent users
    max_overflow=30
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# --- DB Models
class Participant(Base):
    __tablename__ = "participants"
    id = Column(Integer, primary_key=True)
    uuid = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    creative_role = Column(String, nullable=True)
    session_id = Column(String, nullable=True, index=True)  # Session isolation
    created_at = Column(DateTime, server_default=func.now())

    chats = relationship("Chat", back_populates="participant")
    profiles = relationship("ParticipantProfile", back_populates="participant")

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, ForeignKey("participants.id"))
    role = Column(String)  # system/user/assistant
    content = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    stage = Column(String, default="onboarding")  # onboarding | group | cocreation

    participant = relationship("Participant", back_populates="chats")

class ParticipantProfile(Base):
    __tablename__ = "participant_profiles"
    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, ForeignKey("participants.id"))
    current_uses = Column(Text)
    want_to_learn = Column(Text)
    blockers = Column(Text)
    needs_summary = Column(Text)
    embedding = Column(Text)  # JSON string of list[float]

    participant = relationship("Participant", back_populates="profiles")

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)
    group_number = Column(Integer, index=True)  # For simple /group1 URLs
    rationale = Column(Text)

class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    participant_id = Column(Integer, ForeignKey("participants.id"))

class GroupChat(Base):
    __tablename__ = "group_chats"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    role = Column(String)  # system/user/assistant
    content = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    
    group = relationship("Group")

# Create tables (PostgreSQL handles concurrency perfectly)
try:
    Base.metadata.create_all(engine)
    print("✓ PostgreSQL database tables created successfully")
except Exception as e:
    print(f"Database connection info: {DB_URL}")
    print(f"Error creating tables: {e}")
    print("Please ensure PostgreSQL is running and database exists!")

# --- OpenAI client (initialized in functions to avoid startup issues)

def llm_chat(messages: List[Dict[str, str]], model: str = OPENAI_MODEL, temperature: float = 0.4, max_tokens: int = 700) -> str:
    from openai import OpenAI
    import os
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    if model.startswith("gpt-5"):
        # GPT-5 requires Responses API
        # Convert messages to a single prompt for Responses API
        prompt_parts = []
        for msg in messages:
            if msg["role"] == "system":
                prompt_parts.append(f"System: {msg['content']}")
            elif msg["role"] == "user":
                prompt_parts.append(f"User: {msg['content']}")
            elif msg["role"] == "assistant":
                prompt_parts.append(f"Assistant: {msg['content']}")
        
        prompt = "\\n\\n".join(prompt_parts)
        
        resp = client.responses.create(
            model=model,
            input=prompt,
            text={"verbosity": "medium"},
            reasoning={"effort": "minimal"}  # Fastest for workshop use
        )
        return resp.output_text or ""
    else:
        # Use Chat Completions API for other models
        params = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        resp = client.chat.completions.create(**params)
        return resp.choices[0].message.content

def embed(texts: List[str], model: str = EMBED_MODEL) -> List[List[float]]:
    from openai import OpenAI
    import os
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    e = client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in e.data]

# --- HTML Templates (Minimal, inline)
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* {
  box-sizing: border-box;
}

body {
  font-family: 'Inter', system-ui, sans-serif;
  background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 50%, #16213e 100%);
  color: #ffffff;
  margin: 0;
  padding: 0;
  min-height: 100vh;
  line-height: 1.6;
}

.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
  min-height: 100vh;
}

header {
  text-align: center;
  margin-bottom: 48px;
  position: relative;
}

.logo {
  font-size: 48px;
  font-weight: 700;
  letter-spacing: -2px;
  margin-bottom: 8px;
  background: linear-gradient(45deg, #ffffff, #e2e8f0);
  background-clip: text;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  text-transform: uppercase;
}

.subtitle {
  font-size: 20px;
  color: #94a3b8;
  font-weight: 300;
  margin-bottom: 32px;
}

.participant-id {
  font-size: 12px;
  color: #64748b;
  font-weight: 500;
  padding: 8px 16px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 20px;
  display: inline-block;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 24px;
  padding: 32px;
  margin: 24px 0;
  backdrop-filter: blur(10px);
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
}

.chat-container {
  min-height: 400px;
  max-height: 600px;
  overflow-y: auto;
  margin-bottom: 24px;
  padding: 16px;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.msg {
  margin: 16px 0;
  padding: 16px 20px;
  border-radius: 16px;
  position: relative;
  animation: fadeIn 0.3s ease-out;
}

.msg.user {
  background: linear-gradient(135deg, #3b82f6, #1d4ed8);
  margin-left: 48px;
  color: white;
  border-bottom-right-radius: 4px;
}

.msg.ai {
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.1), rgba(255, 255, 255, 0.05));
  margin-right: 48px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-bottom-left-radius: 4px;
}

.msg-header {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
  opacity: 0.8;
}

.msg pre {
  margin: 0;
  white-space: pre-wrap;
  font-family: inherit;
  line-height: 1.5;
}

.input-container {
  display: flex;
  gap: 12px;
  align-items: center;
}

input[type="text"] {
  flex: 1;
  padding: 16px 20px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 16px;
  color: white;
  font-size: 16px;
  font-family: inherit;
  transition: all 0.3s ease;
}

input[type="text"]:focus {
  outline: none;
  border-color: #3b82f6;
  background: rgba(255, 255, 255, 0.12);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
}

input[type="text"]::placeholder {
  color: #64748b;
}

button {
  background: linear-gradient(135deg, #3b82f6, #1d4ed8);
  color: white;
  border: none;
  border-radius: 16px;
  padding: 16px 24px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
  font-family: inherit;
  box-shadow: 0 4px 12px rgba(59, 130, 246, 0.2);
}

button:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(59, 130, 246, 0.3);
  background: linear-gradient(135deg, #2563eb, #1e40af);
}

button:active {
  transform: translateY(0);
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-top: 32px;
}

.badge {
  display: inline-block;
  background: rgba(59, 130, 246, 0.2);
  color: #93c5fd;
  padding: 6px 12px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid rgba(59, 130, 246, 0.3);
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.status-indicator {
  display: inline-block;
  width: 8px;
  height: 8px;
  background: #10b981;
  border-radius: 50%;
  margin-right: 8px;
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

/* Thinking indicator animation */
@keyframes thinking {
  0%, 80%, 100% { opacity: 0.3; }
  40% { opacity: 1; }
}

.thinking-dots::after {
  content: '';
  display: inline-block;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #93c5fd;
  animation: thinking 1.5s infinite;
  margin-left: 4px;
}

.thinking-dots::before {
  content: '••';
  color: #93c5fd;
  animation: thinking 1.5s infinite 0.2s;
}

@media (max-width: 768px) {
  .container { padding: 16px; }
  .logo { font-size: 36px; }
  .card { padding: 24px; margin: 16px 0; }
  .grid { grid-template-columns: 1fr; gap: 16px; }
  .msg.user { margin-left: 16px; }
  .msg.ai { margin-right: 16px; }
}
</style>
"""

PARTICIPANT_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LuovAin! Co-creation Workshop</title>
  {{ base_css|safe }}
</head>
<body>
  <div class="container">
    <header>
      <img src="/static/logo.png" alt="LuovAin!" style="max-width: 300px; height: auto; margin-bottom: 16px;">
      <div class="subtitle">Co-creation with AI • Creative Skills Week</div>
      <div class="participant-id">
        <span class="status-indicator"></span>
        Participant: {{ puuid[:8] }}...
      </div>
    </header>
    
    <div class="card">
      <div id="log" class="chat-container"></div>
      <form id="f" onsubmit="send(event)">
        <div class="input-container">
          <input type="text" id="msg" placeholder="Share your thoughts about creative AI..." autocomplete="off" />
          <button type="submit">Send</button>
        </div>
      </form>
    </div>
  </div>
<script>
const log = document.getElementById('log');
const puuid = '{{ puuid }}';

function add(role, text) {
  const msgDiv = document.createElement('div');
  msgDiv.className = 'msg ' + (role === 'assistant' ? 'ai' : 'user');
  
  const header = document.createElement('div');
  header.className = 'msg-header';
  header.textContent = role === 'assistant' ? '🤖 LuovAin! AI' : '👤 You';
  
  const content = document.createElement('pre');
  content.textContent = text;
  
  msgDiv.appendChild(header);
  msgDiv.appendChild(content);
  log.appendChild(msgDiv);
  
  // Smooth scroll to bottom
  log.scrollTo({ top: log.scrollHeight, behavior: 'smooth' });
}

function escapeHtml(unsafe) {
  return unsafe
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

async function boot() {
  try {
    const response = await fetch('/api/participant/boot/' + puuid);
    const data = await response.json();
    data.messages.forEach(m => add(m.role, m.content));
  } catch (error) {
    console.error('Failed to load initial messages:', error);
  }
}

async function send(e) {
  e.preventDefault();
  const input = document.getElementById('msg');
  const message = input.value.trim();
  if (!message) return;
  
  // Add user message immediately
  add('user', message);
  input.value = '';
  
  // Disable button and show thinking indicator
  const button = document.querySelector('button[type="submit"]');
  const originalText = button.textContent;
  button.textContent = 'AI is thinking...';
  button.disabled = true;
  
  // Add thinking indicator message
  const thinkingId = 'thinking-' + Date.now();
  const thinkingDiv = document.createElement('div');
  thinkingDiv.id = thinkingId;
  thinkingDiv.className = 'msg ai';
  thinkingDiv.style.cssText = 'opacity: 0.7; font-style: italic;';
  thinkingDiv.innerHTML = `
    <div class="msg-header">🤖 LuovAin! AI</div>
    <pre style="color: #93c5fd;">🧠 AI is creating a thoughtful response<span class="thinking-dots"></span></pre>
  `;
  document.getElementById('log').appendChild(thinkingDiv);
  thinkingDiv.scrollIntoView({ behavior: 'smooth' });
  
  try {
    const response = await fetch('/api/participant/chat/' + puuid, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message })
    });
    
    const data = await response.json();
    
    // Remove thinking indicator
    document.getElementById(thinkingId)?.remove();
    
    if (data.reply) {
      add('assistant', data.reply);
    }
  } catch (error) {
    console.error('Failed to send message:', error);
    document.getElementById(thinkingId)?.remove();
    add('assistant', 'Sorry, there was an error. Please try again.');
  } finally {
    button.textContent = originalText;
    button.disabled = false;
    input.focus();
  }
}

// Initialize
boot();

// Focus input on load
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('msg').focus();
});
</script>
"""

FACILITATOR_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LuovAin! Facilitator Dashboard</title>
  {{ base_css|safe }}
</head>
<body>
  <div class="container">
    <header>
      <img src="/static/logo.png" alt="LuovAin!" style="max-width: 300px; height: auto; margin-bottom: 16px;">
      <div class="subtitle">Facilitator Dashboard • Creative Skills Week</div>
      <div class="participant-id">
        <span class="status-indicator"></span>
        <span id="session-status">Workshop Control Center</span>
      </div>
    </header>

    <div class="card" style="margin-bottom:24px;">
      <h3 style="margin-top:0; color:#93c5fd; font-size:20px; margin-bottom:24px;">🎯 Session Management</h3>
      <div style="display:flex; gap:16px; align-items:center; margin-bottom:16px;">
        <button onclick="startNewSession()" style="flex:1;">🚀 Start New Session</button>
        <div id="session-info" style="color:#94a3b8; font-size:14px; flex:2;">No active session</div>
      </div>
      <div id="session-message" style="color:#10b981; font-size:14px; margin-top:16px; min-height:20px;"></div>
    </div>
    
    <div class="grid">
      <div class="card">
        <h3 style="margin-top:0; color:#93c5fd; font-size:20px; margin-bottom:24px;">👥 Participants</h3>
        <div id="plist" style="margin-bottom:16px; min-height:200px;"></div>
        <button onclick="refresh()" style="width:100%;">🔄 Refresh Participants</button>
      </div>
      
      <div class="card">
        <h3 style="margin-top:0; color:#93c5fd; font-size:20px; margin-bottom:24px;">🎯 Form Groups</h3>
        <p style="color:#94a3b8; margin-bottom:16px; font-size:14px;">AI will create balanced 3-person teams</p>
        <button onclick="formGroups()" style="width:100%; margin-bottom:16px;">🚀 Form Groups with GPT-5</button>
        <div id="gout"></div>
        <a href="/groups" target="_blank" style="display:block; background:#059669; color:white; padding:12px; border-radius:8px; text-decoration:none; font-size:14px; font-weight:500; text-align:center; margin-top:16px;">
          📋 Share Group Links
        </a>
      </div>
    </div>
    
    <div class="card" style="margin-top:24px;">
      <h3 style="margin-top:0; color:#93c5fd; font-size:20px; margin-bottom:24px;">🎨 Workshop Themes</h3>
      <p style="color:#94a3b8; margin-bottom:16px; font-size:14px;">Generate discussion themes from participant needs</p>
      <button onclick="topics()" style="margin-bottom:20px;">✨ Generate Themes with AI</button>
      <pre id="themes" style="background:rgba(0,0,0,0.2); padding:16px; border-radius:12px; color:#e2e8f0; font-size:14px; line-height:1.6; border:1px solid rgba(255,255,255,0.1);"></pre>
    </div>
  </div>
<script>
async function refresh() {
  const button = document.querySelector('button[onclick="refresh()"]');
  const originalText = button.textContent;
  button.textContent = '🔄 Loading...';
  button.disabled = true;
  
  try {
    const response = await fetch('/api/facilitator/participants');
    const participants = await response.json();
    const plist = document.getElementById('plist');
    plist.innerHTML = '';
    
    if (participants.length === 0) {
      plist.innerHTML = '<p style="color:#64748b; text-align:center; padding:32px;">No participants yet</p>';
      return;
    }
    
    participants.forEach(p => {
      const div = document.createElement('div');
      div.style.cssText = 'background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:12px; padding:16px; margin:8px 0;';
      div.innerHTML = `
        <div style="font-weight:600; margin-bottom:8px;">
          ${p.name || '(no name yet)'} 
          <span class="badge">${p.uuid.slice(0,8)}...</span>
        </div>
        <div style="color:#94a3b8; font-size:14px;">${p.creative_role || 'Role not specified'}</div>
      `;
      plist.appendChild(div);
    });
  } catch (error) {
    console.error('Failed to load participants:', error);
    document.getElementById('plist').innerHTML = '<p style="color:#ef4444;">Error loading participants</p>';
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function formGroups() {
  const button = document.querySelector('button[onclick="formGroups()"]');
  const originalText = button.textContent;
  button.textContent = '🤖 AI is forming groups...';
  button.disabled = true;
  
  try {
    const response = await fetch('/api/facilitator/form_groups', { method: 'POST' });
    const data = await response.json();
    const gout = document.getElementById('gout');
    gout.innerHTML = '';
    
    if (!data.groups || data.groups.length === 0) {
      gout.innerHTML = '<p style="color:#64748b;">No groups formed. Need at least 3 participants.</p>';
      return;
    }
    
    data.groups.forEach(group => {
      const groupDiv = document.createElement('div');
      groupDiv.style.cssText = 'background:rgba(59,130,246,0.1); border:1px solid rgba(59,130,246,0.3); border-radius:12px; padding:16px; margin:12px 0;';
      
      const membersText = group.participants.join(', ');
      const bulletsHtml = group.rationale_bullets.map(bullet => `<li>${bullet}</li>`).join('');
      
      groupDiv.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom:12px;">
          <h4 style="color:#93c5fd; margin:0; font-size:16px;">Group ${group.number}</h4>
          <span style="background:rgba(59,130,246,0.2); color:#93c5fd; padding:4px 8px; border-radius:8px; font-size:12px;">${group.participants.length} members</span>
        </div>
        <div style="margin-bottom:12px; color:#e2e8f0;">
          <strong>Members:</strong> ${membersText}
        </div>
        ${bulletsHtml ? `<ul style="color:#94a3b8; font-size:14px; margin:8px 0; padding-left:18px;">${bulletsHtml}</ul>` : ''}
        <a href="${group.url}" target="_blank" 
           style="display:inline-block; background:#3b82f6; color:white; padding:8px 16px; border-radius:8px; text-decoration:none; font-size:14px; font-weight:500;">
          🚀 Open Group ${group.number}
        </a>
      `;
      gout.appendChild(groupDiv);
    });
  } catch (error) {
    console.error('Failed to form groups:', error);
    document.getElementById('gout').innerHTML = '<p style="color:#ef4444;">Error forming groups</p>';
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function topics() {
  const button = document.querySelector('button[onclick="topics()"]');
  const originalText = button.textContent;
  button.textContent = '🤖 AI generating themes...';
  button.disabled = true;
  
  try {
    const response = await fetch('/api/facilitator/themes', { method: 'POST' });
    const data = await response.json();
    const themesEl = document.getElementById('themes');
    
    if (data.themes && data.themes.length > 0) {
      let output = '🎨 WORKSHOP THEMES:\\n\\n';
      data.themes.forEach((theme, index) => {
        output += `${index + 1}. ${theme.name}\\n`;
        output += `   ${theme.rationale}\\n`;
        if (theme.representative_quotes) {
          output += `   Quotes: ${theme.representative_quotes.slice(0, 2).join(' • ')}\\n`;
        }
        output += '\\n';
      });
      themesEl.textContent = output;
    } else {
      themesEl.textContent = 'No themes generated. Make sure participants have completed their assessments.';
    }
  } catch (error) {
    console.error('Failed to generate themes:', error);
    document.getElementById('themes').textContent = 'Error generating themes.';
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function startNewSession() {
  const button = document.querySelector('button[onclick="startNewSession()"]');
  const originalText = button.textContent;
  button.textContent = '🚀 Starting new session...';
  button.disabled = true;
  
  try {
    const response = await fetch('/api/facilitator/new_session', { method: 'POST' });
    const data = await response.json();
    
    if (data.success) {
      document.getElementById('session-message').textContent = data.message;
      document.getElementById('session-message').style.color = '#10b981';
      updateSessionStatus();
      // Refresh participants list to show empty list for new session
      refresh();
    } else {
      document.getElementById('session-message').textContent = 'Failed to start new session';
      document.getElementById('session-message').style.color = '#ef4444';
    }
  } catch (error) {
    console.error('Failed to start new session:', error);
    document.getElementById('session-message').textContent = 'Error starting session';
    document.getElementById('session-message').style.color = '#ef4444';
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function updateSessionStatus() {
  try {
    const response = await fetch('/api/facilitator/current_session');
    const data = await response.json();
    
    const sessionInfo = document.getElementById('session-info');
    const sessionStatus = document.getElementById('session-status');
    
    if (data.has_active_session) {
      sessionInfo.textContent = `Active: ${data.current_session_id}`;
      sessionStatus.textContent = `Session: ${data.current_session_id}`;
    } else {
      sessionInfo.textContent = 'No active session';
      sessionStatus.textContent = 'Workshop Control Center';
    }
  } catch (error) {
    console.error('Failed to get session status:', error);
  }
}

// Auto-refresh participants every 5 seconds and update session status
setInterval(() => {
  refresh();
  updateSessionStatus();
}, 5000);
refresh();
updateSessionStatus();
</script>
"""

GROUPS_SHARING_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LuovAin! Groups - Creative Skills Week</title>
  {{ base_css|safe }}
</head>
<body>
  <div class="container">
    <header>
      <img src="/static/logo.png" alt="LuovAin!" style="max-width: 300px; height: auto; margin-bottom: 16px;">
      <div class="subtitle">Workshop Groups • Creative Skills Week</div>
      <div class="participant-id">
        <span class="status-indicator"></span>
        Groups & Access Links
      </div>
    </header>
    
    <div class="card">
      <h3 style="margin-top:0; color:#93c5fd; font-size:20px; margin-bottom:24px;">🚀 Active Groups</h3>
      <p style="color:#94a3b8; margin-bottom:24px; font-size:14px;">Share these links with participants to join their groups</p>
      <div id="groups-list"></div>
      <button onclick="refreshGroups()" style="width:100%; margin-top:20px;">🔄 Refresh Groups</button>
    </div>
  </div>

<script>
async function refreshGroups() {
  const button = document.querySelector('button[onclick="refreshGroups()"]');
  const originalText = button.textContent;
  button.textContent = '🔄 Loading...';
  button.disabled = true;
  
  try {
    const response = await fetch('/api/groups');
    const data = await response.json();
    const groupsList = document.getElementById('groups-list');
    groupsList.innerHTML = '';
    
    if (!data.groups || data.groups.length === 0) {
      groupsList.innerHTML = '<p style="color:#64748b; text-align:center; padding:32px;">No groups formed yet</p>';
      return;
    }
    
    data.groups.forEach(group => {
      const groupDiv = document.createElement('div');
      groupDiv.style.cssText = 'background:rgba(59,130,246,0.1); border:1px solid rgba(59,130,246,0.3); border-radius:12px; padding:20px; margin:16px 0;';
      
      const membersText = group.members.join(', ') || 'No members yet';
      const bulletsHtml = group.rationale_bullets.map(bullet => `<li>${bullet}</li>`).join('');
      const fullUrl = window.location.origin + group.url;
      
      groupDiv.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom:12px;">
          <h4 style="color:#93c5fd; margin:0; font-size:18px;">Group ${group.number}</h4>
          <span style="background:rgba(59,130,246,0.2); color:#93c5fd; padding:4px 12px; border-radius:12px; font-size:12px;">${group.members.length} members</span>
        </div>
        <div style="margin-bottom:12px;">
          <strong style="color:#e2e8f0;">Members:</strong> <span style="color:#cbd5e1;">${membersText}</span>
        </div>
        ${bulletsHtml ? `<ul style="color:#94a3b8; font-size:14px; margin:12px 0; padding-left:20px;">${bulletsHtml}</ul>` : ''}
        <div style="background:rgba(0,0,0,0.2); padding:12px; border-radius:8px; margin:12px 0;">
          <strong style="color:#e2e8f0; font-size:14px;">Group Link:</strong><br>
          <code style="color:#93c5fd; font-size:16px; word-break:break-all;">${fullUrl}</code>
        </div>
        <div style="display: flex; gap: 12px; margin-top:16px;">
          <a href="${group.url}" target="_blank" 
             style="flex:1; display:block; background:#3b82f6; color:white; padding:12px; border-radius:8px; text-decoration:none; font-size:14px; font-weight:500; text-align:center;">
            🚀 Open Group Chat
          </a>
          <button onclick="copyLink('${fullUrl}')" 
                  style="background:#059669; padding:12px 16px; border-radius:8px; font-size:14px;">
            📋 Copy Link
          </button>
        </div>
      `;
      groupsList.appendChild(groupDiv);
    });
  } catch (error) {
    console.error('Failed to load groups:', error);
    document.getElementById('groups-list').innerHTML = '<p style="color:#ef4444;">Error loading groups</p>';
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

function copyLink(url) {
  navigator.clipboard.writeText(url).then(() => {
    // Show brief success feedback
    const event = new CustomEvent('show-toast', {detail: {message: 'Link copied!', type: 'success'}});
    document.dispatchEvent(event);
  }).catch(() => {
    // Fallback for older browsers
    const textArea = document.createElement('textarea');
    textArea.value = url;
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand('copy');
    document.body.removeChild(textArea);
  });
}

// Auto-refresh every 10 seconds
setInterval(refreshGroups, 10000);
refreshGroups();
</script>
"""

GROUP_HTML = """
<!doctype html>
<title>Group Co-creation – {{ gname }}</title>
{{ base_css|safe }}
<header>
  <h1>Group: {{ gname }}</h1>
  <div class="small">One person acts as scribe. Keep answers short. Aim to finish in 30 minutes.</div>
</header>
<div class="card">
  <div id="log"></div>
  <form id="f" onsubmit="send(event)">
    <div style="display:flex;gap:8px">
      <input id="msg" placeholder="Type your reply…" style="flex:1" autocomplete="off" />
      <button>Send</button>
    </div>
  </form>
</div>
<script>
const log = document.getElementById('log');
const gname = '{{ gname }}';
function add(role, text){
  const d=document.createElement('div');
  d.className = 'msg ' + (role==='assistant'?'ai':'user');
  d.innerHTML = '<b>'+(role==='assistant'?'AI Chair':'Group')+':</b> <pre>'+escapeHtml(text)+'</pre>';
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}
function escapeHtml(unsafe) {return unsafe
  .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
async function boot(){
  const r = await fetch('/api/group/boot/'+encodeURIComponent(gname));
  const j = await r.json();
  j.messages.forEach(m=> add(m.role,m.content));
}
async function send(e){
  e.preventDefault();
  const v = document.getElementById('msg').value.trim(); if(!v) return;
  add('user', v); document.getElementById('msg').value='';
  const r = await fetch('/api/group/chat/'+encodeURIComponent(gname),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})});
  const j = await r.json();
  add('assistant', j.reply);
}
boot();
</script>
"""

# --- Helpers

def get_or_create_participant(puuid: str) -> Participant:
    db = SessionLocal()
    p = db.query(Participant).filter_by(uuid=puuid).first()
    if not p:
        p = Participant(uuid=puuid, session_id=CURRENT_SESSION_ID)
        db.add(p); db.commit()
    db.close()
    return p

# --- Routes: Participant onboarding + dialogue
@app.route("/")
def participant_entry():
    puuid = str(uuid.uuid4())
    return render_template_string(PARTICIPANT_HTML, base_css=BASE_CSS, puuid=puuid)

@app.get("/api/participant/boot/<puuid>")
def participant_boot(puuid):
    # Seed with system + first AI question
    db = SessionLocal()
    p = db.query(Participant).filter_by(uuid=puuid).first()
    if not p:
        p = Participant(uuid=puuid, session_id=CURRENT_SESSION_ID)
        db.add(p); db.commit()
    # system
    db.add(Chat(participant_id=p.id, role="system", content=SYSTEM_INSTRUCTIONS_PARTICIPANT, stage="onboarding"))
    # assistant first prompt
    first = "Hi! Let's get started.\n1) Your name?\n2) What kind of creative work do you do (profession or hobby)?"
    db.add(Chat(participant_id=p.id, role="assistant", content=first, stage="onboarding"))
    db.commit()
    db.close()
    return jsonify({"messages":[{"role":"assistant","content":first}]})

@app.post("/api/participant/chat/<puuid>")
def participant_chat(puuid):
    db = SessionLocal()
    p: Participant = db.query(Participant).filter_by(uuid=puuid).first()
    if not p:
        db.close()
        return jsonify({"error":"participant not found"}), 404

    user_msg = request.json.get("message", "").strip()
    db.add(Chat(participant_id=p.id, role="user", content=user_msg, stage="onboarding"))

    # Build context
    messages = []
    chats = db.query(Chat).filter_by(participant_id=p.id, stage="onboarding").order_by(Chat.id.asc()).all()
    for c in chats:
        messages.append({"role": c.role, "content": c.content})

    # Count user exchanges to determine if we should wrap up
    user_exchanges = len([c for c in chats if c.role == 'user'])
    should_conclude = user_exchanges >= 5  # After 5+ user messages, conclude
    
    if should_conclude:
        # Add instruction to conclude
        messages.append({
            "role": "system", 
            "content": "The participant has answered enough questions. Now conclude with 'Thanks [name]! Here's your upskilling profile:' followed by a bullet summary of their needs, then 'You're ready to connect with others for co-creation! The facilitator will form groups soon.'"
        })

    # Run model
    reply = llm_chat(messages)
    db.add(Chat(participant_id=p.id, role="assistant", content=reply, stage="onboarding"))

    # Try to extract name/role after every user message (adaptive approach)
    if user_exchanges >= 1:  # After any user input
        try:
            conversation_text = "\n".join([f"{c.role}: {c.content}" for c in chats])
            extraction_prompt = f"""Analyze this conversation and extract any name or creative role mentioned by the user.

Conversation:
{conversation_text}

Look for:
- User's name (first name, full name, etc.)
- Creative profession/role (photographer, musician, designer, etc.)

Return JSON with exactly these fields:
{{"name": "extracted_name_or_null", "role": "extracted_role_or_null"}}

Examples:
- User says "karen" → {{"name": "karen", "role": null}}
- User says "I'm a photographer" → {{"name": null, "role": "photographer"}}
- User says "John, I do music" → {{"name": "John", "role": "music"}}

If nothing clear is found, use null."""

            extraction_messages = [
                {"role": "system", "content": "You are a data extraction assistant. Return only valid JSON."},
                {"role": "user", "content": extraction_prompt}
            ]
            
            extraction_result = llm_chat(extraction_messages, temperature=0.1, max_tokens=200)
            
            import json
            extracted = json.loads(extraction_result)
            
            # Update name if found and not already set
            if extracted.get("name") and not p.name:
                extracted_name = str(extracted["name"]).strip()[:120]
                if len(extracted_name) > 1:
                    p.name = extracted_name
                    print(f"✓ Extracted name: '{extracted_name}' for participant {puuid[:8]}")
            
            # Update role if found and not already set
            if extracted.get("role") and not p.creative_role:
                extracted_role = str(extracted["role"]).strip()[:200]
                if len(extracted_role) > 1:
                    p.creative_role = extracted_role
                    print(f"✓ Extracted role: '{extracted_role}' for participant {puuid[:8]}")
                    
        except Exception as e:
            print(f"Failed to extract name/role: {e}")

    # Light extraction: when the assistant posts a summary, try to capture a profile
    if "upskilling profile" in reply.lower() or "You're ready to connect" in reply or should_conclude:
        # Create/update profile (very naive extraction – in practice, add a separate extraction pass)
        prof = db.query(ParticipantProfile).filter_by(participant_id=p.id).first()
        if not prof:
            prof = ParticipantProfile(participant_id=p.id)
            db.add(prof)
        prof.needs_summary = reply
        # Final extraction at conversation end (only if still missing info)
        if not p.name or not p.creative_role:
            try:
                conversation_text = "\n".join([f"{c.role}: {c.content}" for c in chats])
                extraction_prompt = f"""Extract missing participant information from this conversation.

Current status:
- Name: {'found' if p.name else 'missing'}
- Role: {'found' if p.creative_role else 'missing'}

Conversation:
{conversation_text}

Return JSON with exactly these fields:
{{"name": "extracted_name_or_null", "role": "extracted_role_or_null"}}

Only extract what is currently missing. If already found, return null for that field."""

                extraction_messages = [
                    {"role": "system", "content": "You are a data extraction assistant. Return only valid JSON."},
                    {"role": "user", "content": extraction_prompt}
                ]
                
                extraction_result = llm_chat(extraction_messages, temperature=0.1, max_tokens=200)
                
                import json
                extracted = json.loads(extraction_result)
                
                if extracted.get("name") and not p.name:
                    extracted_name = str(extracted["name"]).strip()[:120]
                    if len(extracted_name) > 1:
                        p.name = extracted_name
                        print(f"✓ Final name extraction: '{extracted_name}' for participant {puuid[:8]}")
                
                if extracted.get("role") and not p.creative_role:
                    extracted_role = str(extracted["role"]).strip()[:200]
                    if len(extracted_role) > 1:
                        p.creative_role = extracted_role
                        print(f"✓ Final role extraction: '{extracted_role}' for participant {puuid[:8]}")
                        
            except Exception as e:
                print(f"Failed final extraction: {e}")
        # Note: embeddings no longer needed for GPT-5 based grouping
    db.commit(); db.close()
    return jsonify({"reply": reply})

# --- Facilitator dashboard
@app.route("/facilitator")
def facilitator():
    return render_template_string(FACILITATOR_HTML, base_css=BASE_CSS)

@app.get("/groups")
def groups_sharing_page():
    return render_template_string(GROUPS_SHARING_HTML, base_css=BASE_CSS)

@app.get("/api/groups")
def get_groups_info():
    db = SessionLocal()
    groups = db.query(Group).order_by(Group.group_number).all()
    groups_info = []
    
    for group in groups:
        # Get group members
        members = db.query(Participant).join(GroupMember).filter(GroupMember.group_id == group.id).all()
        member_names = [m.name for m in members if m.name]
        
        # Format rationale as bullet points (max 3)
        rationale_bullets = []
        if group.rationale:
            # Try to split by sentences or key points
            sentences = group.rationale.split('. ')
            for i, sentence in enumerate(sentences[:3]):
                if sentence.strip():
                    rationale_bullets.append(sentence.strip().rstrip('.'))
        
        groups_info.append({
            "number": group.group_number,
            "name": group.name,
            "members": member_names,
            "rationale_bullets": rationale_bullets,
            "url": f"/group{group.group_number}"
        })
    
    db.close()
    return jsonify({"groups": groups_info})

@app.get("/api/facilitator/participants")
def facilitator_participants():
    db = SessionLocal()
    # Show participants who have interacted (have user chats) in current session
    query = db.query(Participant).join(Chat, Participant.id == Chat.participant_id)\
        .filter(Chat.role == 'user')\
        .distinct(Participant.id)
    
    # Filter by current session if one is active
    if CURRENT_SESSION_ID:
        query = query.filter(Participant.session_id == CURRENT_SESSION_ID)
    
    rows = query.order_by(Participant.id.desc()).all()
    out = []
    for r in rows:
        # Try to extract name from recent user messages if not stored
        display_name = r.name
        display_role = r.creative_role
        
        if not display_name:
            recent_chats = db.query(Chat).filter_by(participant_id=r.id, role='user')\
                .order_by(Chat.id.desc()).limit(5).all()
            for chat in recent_chats:
                if chat.content and len(chat.content.strip()) > 0:
                    # Try to extract name from first meaningful line
                    lines = [line.strip() for line in chat.content.split('\n') if line.strip()]
                    if lines and len(lines[0]) <= 50 and not lines[0].lower().startswith(('hei', 'hello', 'hi', 'oletko')):
                        display_name = lines[0]
                        break
        
        out.append({
            "uuid": r.uuid, 
            "name": display_name or "(chatting...)", 
            "creative_role": display_role or "Role being discussed"
        })
    db.close()
    return jsonify(out)

@app.post("/api/facilitator/new_session")
def facilitator_new_session():
    global CURRENT_SESSION_ID
    import datetime
    CURRENT_SESSION_ID = datetime.datetime.now().strftime("session_%Y%m%d_%H%M%S")
    return jsonify({
        "success": True, 
        "session_id": CURRENT_SESSION_ID,
        "message": f"New session '{CURRENT_SESSION_ID}' started. New participants will join this session."
    })

@app.get("/api/facilitator/current_session")
def facilitator_current_session():
    return jsonify({
        "current_session_id": CURRENT_SESSION_ID,
        "has_active_session": CURRENT_SESSION_ID is not None
    })

@app.post("/api/facilitator/themes")
def facilitator_themes():
    db = SessionLocal()
    # Only include profiles from current session participants
    query = db.query(ParticipantProfile).join(Participant, ParticipantProfile.participant_id == Participant.id)
    if CURRENT_SESSION_ID:
        query = query.filter(Participant.session_id == CURRENT_SESSION_ID)
    
    profs = query.all()
    texts = [p.needs_summary for p in profs if p.needs_summary]
    if not texts:
        db.close(); return jsonify({"themes": []})

    joined = "\n---\n".join(texts)
    messages = [
        {"role":"system","content":SYSTEM_INSTRUCTIONS_SUMMARY_TO_TOPICS},
        {"role":"user","content":joined}
    ]
    reply = llm_chat(messages, temperature=0.2, max_tokens=800)
    db.close()
    try:
        data = json.loads(reply)
    except Exception:
        data = {"themes": []}
    return jsonify(data)

# --- Grouping logic: GPT-5 based intelligent grouping

@app.post("/api/facilitator/form_groups")
def form_groups():
    try:
        db = SessionLocal()
        # Get all participants with name and role (regardless of profile completion)
        query = db.query(Participant)
        if CURRENT_SESSION_ID:
            query = query.filter(Participant.session_id == CURRENT_SESSION_ID)
        
        participants = query.all()
        people = []
        for p in participants:
            # Include participants who have at least a name and role
            if not (p.name and p.creative_role):
                continue
                
            # Try to get their profile if it exists
            prof = db.query(ParticipantProfile).filter_by(participant_id=p.id).first()
            
            people.append({
                "name": p.name, 
                "role": p.creative_role, 
                "needs_summary": prof.needs_summary if prof and prof.needs_summary else f"Creative professional working in {p.creative_role}"
            })

        print(f"Found {len(people)} participants ready for grouping:")
        for p in people:
            print(f"  - {p['name']} ({p['role']})")
            
        if len(people) < 3:
            print(f"Not enough participants for grouping (need 3, have {len(people)})")
            db.close(); return jsonify({"groups": []})

        # Prepare participant profiles for GPT-5
        profiles_text = "\n\n".join([
            f"**{p['name']}**\nRole: {p['role']}\nNeeds Summary: {p['needs_summary']}"
            for p in people
        ])
        
        # Use GPT-5 for intelligent grouping
        messages = [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS_GROUPING},
            {"role": "user", "content": f"Form groups from these {len(people)} participants:\n\n{profiles_text}"}
        ]
        
        try:
            reply = llm_chat(messages, temperature=0.3, max_tokens=1500)
            print(f"GPT-5 grouping response: {reply}")
            
            # Try to extract JSON from response (sometimes GPT-5 adds extra text)
            import re
            json_match = re.search(r'\{.*\}', reply, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                groups_data = json.loads(json_str)
            else:
                groups_data = json.loads(reply)
                
            groups = groups_data.get("groups", [])
            print(f"Successfully parsed {len(groups)} groups")
        except Exception as e:
            print(f"GPT-5 grouping failed: {e}")
            print(f"Raw response was: {reply if 'reply' in locals() else 'No response received'}")
            # Fallback: simple round-robin grouping
            groups = []
            for i in range(0, len(people), 3):
                if i + 2 < len(people):
                    groups.append({
                        "name": f"Group {len(groups)+1}",
                        "participants": [people[j]["name"] for j in range(i, min(i+3, len(people)))],
                        "rationale": "Simple grouping (AI analysis failed)"
                    })

        # Persist groups
        # Clear old (in correct order due to foreign keys)
        db.query(GroupChat).delete(); db.query(GroupMember).delete(); db.query(Group).delete(); db.commit()
        for i, g in enumerate(groups):
            group_number = i + 1
            row = Group(name=g["name"], group_number=group_number, rationale=g["rationale"])
            db.add(row); db.commit()
            for pname in g["participants"]:
                pid = db.query(Participant).filter_by(name=pname).first()
                if pid:
                    db.add(GroupMember(group_id=row.id, participant_id=pid.id))
            db.commit()
        
        # Return enhanced group data with numbers and formatted rationale
        enhanced_groups = []
        for i, g in enumerate(groups):
            group_number = i + 1
            
            # Format rationale as bullet points (max 3)
            rationale_bullets = []
            if g.get("rationale"):
                sentences = g["rationale"].split('. ')
                for j, sentence in enumerate(sentences[:3]):
                    if sentence.strip():
                        rationale_bullets.append(sentence.strip().rstrip('.'))
            
            enhanced_groups.append({
                "name": g["name"],
                "number": group_number,
                "participants": g["participants"],
                "rationale": g["rationale"],
                "rationale_bullets": rationale_bullets,
                "url": f"/group{group_number}"
            })
    
        db.close()
        return jsonify({"groups": enhanced_groups})
    
    except Exception as main_error:
        print(f"Main form_groups error: {main_error}")
        import traceback
        traceback.print_exc()
        try:
            db.close()
        except:
            pass
        return jsonify({"error": f"Failed to form groups: {str(main_error)}"}), 500

# --- Group co-creation pages
@app.get("/group/<gname>")
def group_page(gname):
    return render_template_string(GROUP_HTML, base_css=BASE_CSS, gname=gname)

@app.get("/group<int:group_num>")
def group_page_by_number(group_num):
    db = SessionLocal()
    group = db.query(Group).filter_by(group_number=group_num).first()
    db.close()
    if not group:
        return f"Group {group_num} not found", 404
    return render_template_string(GROUP_HTML, base_css=BASE_CSS, gname=group.name)

@app.get("/api/group/boot/<gname>")
@app.get("/api/group<int:group_num>/boot")
def group_boot(gname=None, group_num=None):
    db = SessionLocal()
    
    if group_num is not None:
        group = db.query(Group).filter_by(group_number=group_num).first()
    else:
        group = db.query(Group).filter_by(name=gname).first()
    
    if not group:
        db.close()
        return jsonify({"error": "Group not found"}), 404
    
    # Get existing conversation history
    chat_history = db.query(GroupChat).filter_by(group_id=group.id).order_by(GroupChat.created_at).all()
    
    # If this is first boot, create initial message and save it
    if not chat_history:
        if group.rationale:
            contextual_intro = f"Welcome to your co-creation group! Based on your profiles, you were grouped together because: {group.rationale}\n\nThis alignment gives you unique synergy opportunities. Let's leverage these connections as we work together."
            first = f"{contextual_intro}\n\nWhich concept do you want to create now (e.g., short film script, song lyrics, software idea, event concept, game plot)?"
        else:
            first = "Which concept do you want to create now (e.g., short film script, song lyrics, software idea, event concept, game plot)?"
        
        # Save initial message to database
        initial_chat = GroupChat(group_id=group.id, role="assistant", content=first)
        db.add(initial_chat)
        db.commit()
        
        messages = [{"role": "assistant", "content": first}]
    else:
        # Return existing conversation history
        messages = [{"role": chat.role, "content": chat.content} for chat in chat_history]
    
    db.close()
    return jsonify({"messages": messages})

@app.post("/api/group/chat/<gname>")
@app.post("/api/group<int:group_num>/chat")
def group_chat(gname=None, group_num=None):
    db = SessionLocal()
    
    if group_num is not None:
        group = db.query(Group).filter_by(group_number=group_num).first()
    else:
        group = db.query(Group).filter_by(name=gname).first()
    
    if not group:
        db.close()
        return jsonify({"error": "Group not found"}), 404
    
    user_msg = request.json.get("message", "")
    
    # Save user message to database
    user_chat = GroupChat(group_id=group.id, role="user", content=user_msg)
    db.add(user_chat)
    db.commit()
    
    # Get conversation history including the just-added user message
    chat_history = db.query(GroupChat).filter_by(group_id=group.id).order_by(GroupChat.created_at).all()
    
    # Create enhanced system prompt with group synergy context
    enhanced_system_prompt = SYSTEM_INSTRUCTIONS_GROUP_CHAIR
    if group.rationale:
        enhanced_system_prompt += f"\n\nIMPORTANT CONTEXT: This group was formed because: {group.rationale}\nLeverage these synergies and guide the team to build on their complementary strengths and shared interests."
    
    # Build full conversation context for stateful AI
    messages = [{"role": "system", "content": enhanced_system_prompt}]
    
    # Add conversation history (skip initial assistant message to avoid duplication)
    for chat in chat_history:
        if chat.role != "system":  # Skip system messages in history
            messages.append({"role": chat.role, "content": chat.content})
    
    # Get AI response
    reply = llm_chat(messages, temperature=0.5, max_tokens=900)
    
    # Save AI response to database
    ai_chat = GroupChat(group_id=group.id, role="assistant", content=reply)
    db.add(ai_chat)
    db.commit()
    
    db.close()
    return jsonify({"reply": reply})

# --- Minimal consent & export endpoints (stubs to extend)
@app.get("/consent")
def consent_page():
    return "<h1>Consent</h1><p>This pilot stores your name and dialogue for research with strict privacy. Ask a facilitator if you have questions.</p>"

@app.get("/export_json")
def export_json():
    db = SessionLocal()
    data = {
        "participants": [],
        "chats": [],
        "profiles": []
    }
    for p in db.query(Participant).all():
        data["participants"].append({"uuid": p.uuid, "name": p.name, "creative_role": p.creative_role, "created_at": str(p.created_at)})
    for c in db.query(Chat).all():
        data["chats"].append({"participant_uuid": db.query(Participant).get(c.participant_id).uuid, "role": c.role, "content": c.content, "stage": c.stage, "created_at": str(c.created_at)})
    for pr in db.query(ParticipantProfile).all():
        data["profiles"].append({"participant_uuid": db.query(Participant).get(pr.participant_id).uuid, "needs_summary": pr.needs_summary})
    db.close()
    return jsonify(data)

if __name__ == "__main__":
    # Production-ready settings for external access
    app.run(host="0.0.0.0", port=8000, debug=False)

# --------------------
# Research / Ethics TODOs (short)
# --------------------
# 1) Add explicit consent screen before onboarding; store a boolean consent flag.
# 2) Pseudonymise names (hash) and store mapping separately; show only display names to facilitators.
# 3) Add a proper extraction step for (current_uses, want_to_learn, blockers) using a deterministic prompt
#    and store those fields to participant_profiles.
# 4) Persist group chat logs to DB (stage='cocreation') per group with timestamps.
# 5) Add rate limits and abuse filters; log model+version for reproducibility.
# 6) Add downloadable CSVs for quick post-workshop analysis.
# 7) Internationalisation (UI strings fi/en), accessibility, and mobile layout.
# 8) Swap to Responses API if preferred; keep prompts identical.
