# Memory Layers (Symbiosis-Memory)

This document describes the core philosophy of **Symbiosis-Memory**: building human-like **short-term, mid-term, and long-term memory layers**, combined with **automatic classification** and **forgetting mechanisms**.

---

## 🧠 Short-Term Memory

**Definition**  
Short-term memory = immediate working memory, only existing within the current conversation.  

**AI Implementation**  
- Stores the **last 100 dialogue turns**.  
- Automatically classifies conversation topics (e.g., gaming, daily life, creativity, hobbies, sports) — **no human intervention needed**.  
- AI autonomously extracts and records relevant content.  
- Frequently recurring information is gradually **promoted to Mid-Term Memory**.  

---

## 📆 Mid-Term Memory

**Definition**  
Mid-term memory = information that frequently reappears in short-term memory and needs to be retained longer.  

**AI Implementation**  
- Scope: remembers content across the entire chat window.  
- When the token budget reaches its limit:  
  - **Frequently repeated content** → promoted to Long-Term Memory  
  - **Rarely mentioned content** → deleted to free space  
- Example:  
  - If the user repeatedly calls the AI “Zina,” the model infers it is the AI’s name → stored in Long-Term Memory.  

---

## 📚 Long-Term Memory

**Definition**  
Long-term memory = deeply consolidated identity and core knowledge, such as:  
- My name  
- Your name  
- Core skills or traits  

**AI Implementation**  
- Stores only **deeply reinforced, continuously used information**.  
- If an item is not accessed for a long time:  
  - It is **not immediately deleted**.  
  - The system first checks **Mid-Term** and **Short-Term Memory** for related mentions.  
  - If completely unused, it will gradually decay or be removed.  

---

## 🌊 Forgetting & Retrieval Mechanisms

1. **Short-Term → Mid-Term**: Based on frequency, recurring content is promoted.  
2. **Mid-Term → Long-Term**: Daily or repeatedly reinforced information becomes core identity.  
3. **Mid-Term Cleanup**: When memory is full, low-frequency data is removed.  
4. **Long-Term Decay**: Items not accessed for a long time are checked across layers before being faded or deleted.  

---

## 🔄 Memory Flow Diagram

[ Short-Term (100 turns) ]
│
▼
(Frequent mentions)
│
▼
[ Mid-Term (chat window limit) ]
│
┌────┴────┐
▼ ▼
Promote Delete (rarely used)
│
▼
[ Long-Term (identity, skills, names) ]
│
(Decay check)
│
▼
Remove or Archive


---

## 🎯 Summary

- **Short-Term**: working memory (100 turns), auto-classified  
- **Mid-Term**: window-level memory, limited by token budget, frequency-based filtering  
- **Long-Term**: stable identity and knowledge, preserved only if continuously reinforced  
- **Forgetting Mechanisms**: dynamic adjustment ensures growth without uncontrolled expansion
