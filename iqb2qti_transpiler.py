import argparse
import re
import uuid
import os
import shutil
import zipfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Union
from lxml import etree

QTI_NS = "http://www.imsglobal.org/xsd/imsqtiv3p0_v1p0"

@dataclass
class MatchSource:
    id: str
    text: str
    target_id: Optional[str] = None

@dataclass
class QItem:
    seq_num: int
    item_id: str
    q_type: str = 'MCQ'
    prompt: str = ""
    solution: str = ""
    topic: str = "General"
    difficulty: int = 2
    weight: float = 1.0
    shuffle: bool = True
    choices: List[Dict] = field(default_factory=list)
    match_targets: List[Dict] = field(default_factory=list) 
    match_sources: List[MatchSource] = field(default_factory=list) 
    score_line: List[float] = field(default_factory=list)
    solution_label: str = ""

@dataclass
class QSection:
    title: str
    identifier: str
    children: List[Union['QSection', QItem]] = field(default_factory=list)
    rubric: str = ""
    start_q: int = 0
    end_q: int = 0

# ==========================================
# Parser Engine
# ==========================================

class IQBParser:
    def __init__(self, file_content: List[str]):
        self.lines = file_content
        self.idx = 0
        self.item_counter = 0
        self.sys_meta = {"d": 2, "w": 1.0}
        self.root = QSection("Root", "root")
        self.current_sec_node = None
        self.current_part_node = None

    def is_q_start(self, l):
        if not l: return False
        return l.startswith('#') or re.match(r'^(Question|Câu)\s+\d+', l, re.I)

    def run(self):
        while self.idx < len(self.lines):
            l = self.lines[self.idx].strip()
            if not l: self.idx += 1; continue

            if l.startswith('~'):
                self.current_sec_node = QSection(re.sub(r'^[~]\s*|\{.*?\}|\(.*?\)', '', l).strip(), f"s_{uuid.uuid4().hex[:4]}")
                self.root.children.append(self.current_sec_node)
                self.current_part_node = None; self.idx += 1
            elif l.startswith('@'):
                self.ensure_sec()
                self.current_part_node = QSection(re.sub(r'^[@]\s*|\{.*?\}|\(.*?\)', '', l).strip(), f"p_{uuid.uuid4().hex[:4]}")
                self.current_sec_node.children.append(self.current_part_node); self.idx += 1
            elif l.startswith('##'):
                self.ensure_part()
                self.current_part_node.children.append(self.parse_group())
            elif self.is_q_start(l):
                self.ensure_part()
                self.current_part_node.children.append(self.parse_item())
            else: self.idx += 1
        return self.root

    def ensure_sec(self):
        if not self.current_sec_node:
            self.current_sec_node = QSection("Bank", "s_def"); self.root.children.append(self.current_sec_node)

    def ensure_part(self):
        if not self.current_part_node:
            self.current_part_node = QSection("Questions", "p_def"); self.current_sec_node.children.append(self.current_part_node)

    def parse_item(self) -> QItem:
        self.item_counter += 1
        line = self.lines[self.idx]
        prefix_m = re.match(r'^(#|(Question|Câu)\s+[\d\.]*)', line, re.I)
        prefix = prefix_m.group(0) if prefix_m else ""
        
        item = QItem(seq_num=self.item_counter, item_id=f"it_{uuid.uuid4().hex[:6]}")
        item.prompt = line[len(prefix):].strip()
        self.idx += 1
        
        last_target_id = None
        mode = "PROMPT"

        while self.idx < len(self.lines):
            l = self.lines[self.idx].strip()
            if not l: self.idx += 1; continue
            if (self.is_q_start(l) or l.startswith(('~', '@', '$', '##'))) and not l.startswith('/#'): break
            if l.startswith('/#'): break 

            if l.startswith('/='):
                mode = "SOLUTION"; item.solution += l[2:].strip() + "\n"; self.idx += 1; continue

            if l.startswith('/'):
                scores = re.findall(r'[\d\.]+', l)
                if len(scores) > 1: item.score_line = [float(s) for s in scores]
                else: item.solution_label = l[1:].strip()
                self.idx += 1; continue

            if l.startswith('*'):
                item.q_type = 'MATCH'
                cat_content = l[1:].strip()
                cid = f"cat_{len(item.match_targets)}"
                item.match_targets.append({'id': cid, 'text': cat_content or "Correct"})
                last_target_id = cid
                self.idx += 1; continue

            if l.startswith('^'):
                item.q_type = 'MATCH'
                x_match = re.search(r'\s*\[x\]$', l, re.I)
                clean_text = l[1:x_match.start()].strip() if x_match else l[1:].strip()
                
                target_id = None
                if len(item.match_targets) <= 1:
                    item.shuffle = False
                    if x_match: target_id = item.match_targets[0]['id'] if item.match_targets else "cat_0"
                else:
                    item.shuffle = True
                    target_id = last_target_id

                item.match_sources.append(MatchSource(f"src_{len(item.match_sources)}", clean_text, target_id))
                self.idx += 1; continue

            if re.match(r'^[A-Z][\.\)]|^\([A-Z]\)', l):
                if ')' in l or '(' in l: item.shuffle = False
                lbl_m = re.match(r'^[\(\[]?([A-Z])[\)\]\.]?', l)
                txt = re.sub(r'^[\(\[]?[A-Z][\)\]\.]?', '', l).strip()
                item.choices.append({'label': lbl_m.group(1) if lbl_m else "", 'text': txt.replace('[x]', '').strip(), 'correct': '[x]' in l})
                self.idx += 1; continue

            if mode == "PROMPT" and not item.match_targets: item.prompt += "\n" + l
            elif mode == "SOLUTION": item.solution += l + "\n"
            self.idx += 1
        
        item.prompt = item.prompt.strip()
        return item

    def parse_group(self) -> QSection:
        self.idx += 1 
        rubric_lines = []
        while self.idx < len(self.lines) and not self.lines[self.idx].strip().startswith('//'):
            rubric_lines.append(self.lines[self.idx])
            self.idx += 1
        
        if self.idx < len(self.lines) and self.lines[self.idx].strip().startswith('//'):
            self.idx += 1 
            
        grp = QSection("Group", f"g_{uuid.uuid4().hex[:4]}", 
                       rubric="\n".join(rubric_lines).strip(), start_q=self.item_counter + 1)
        

        while self.idx < len(self.lines) and not self.lines[self.idx].strip().startswith('/#'):
            l = self.lines[self.idx].strip()
            if self.is_q_start(l):
                grp.children.append(self.parse_item())
            else:
                self.idx += 1
        
        if self.idx < len(self.lines) and self.lines[self.idx].strip().startswith('/#'):
            self.idx += 1 
            
        grp.end_q = self.item_counter
        return grp

# ==========================================
# Builder Engine (Standard QTI Mapping)
# ==========================================

class QTI3FinalBuilder:
    def build(self, root_node, input_path):
        temp_dir = Path("qti_production")
        if temp_dir.exists(): shutil.rmtree(temp_dir)
        temp_dir.mkdir()

        test_root = etree.Element("assessmentTest", nsmap={None: QTI_NS}, identifier="iQB_Bank")
        tp = etree.SubElement(test_root, "testPart", identifier="tp1", navigationMode="nonlinear", submissionMode="individual")
        
        def walk(node, parent_xml):
            for child in node.children:
                if isinstance(child, QSection):
                    # assessmentSection may contain rubricBlock
                    sec = etree.SubElement(parent_xml, "assessmentSection", identifier=child.identifier, title=child.title, visible="true")
                    if child.rubric:
                        rubric = etree.SubElement(sec, "rubricBlock", view="candidate")
                        etree.SubElement(rubric, "div").text = child.rubric
                    walk(child, sec)
                else:
                    fn = f"item_{child.seq_num:03d}.xml"
                    self.write_item(child, temp_dir, fn)
                    etree.SubElement(parent_xml, "assessmentItemRef", identifier=f"ref_{child.seq_num}", href=fn)

        walk(root_node, tp)
        with open(temp_dir / "assessment.xml", 'wb') as f:
            f.write(etree.tostring(test_root, pretty_print=True, xml_declaration=True, encoding='UTF-8'))

        zip_fn = f"{input_path.stem}.zip"
        with zipfile.ZipFile(zip_fn, 'w') as zf:
            for f in temp_dir.glob("*.xml"): zf.write(f, f.name)
        shutil.rmtree(temp_dir)
        print(f"[SUCCESS] Package created: {zip_fn}")

    def write_item(self, item, folder, filename):
        root = etree.Element("assessmentItem", nsmap={None: QTI_NS}, identifier=item.item_id, title=f"Q{item.seq_num}")
        
        # 1. Metadata
        meta = etree.SubElement(root, "itemMetadata")
        q_meta = etree.SubElement(meta, "qtiMetadata")
        for k, v in [("Topic", item.topic), ("Difficulty", str(item.difficulty)), ("Weight", str(item.weight))]:
            f = etree.SubElement(q_meta, "qtiMetadataField"); etree.SubElement(f, "label").text = k; etree.SubElement(f, "entry").text = v

        # 2. Score Lookup Table
        if item.score_line:
            od = etree.SubElement(root, "outcomeDeclaration", identifier="SCORE", cardinality="single", baseType="float")
            lt = etree.SubElement(od, "lookupTable", defaultValue="0")
            for i, score in enumerate(item.score_line):
                etree.SubElement(lt, "integerTableEntry", sourceValue=str(i), targetValue=str(score))

        # 3. Response Declaration
        card = "multiple" if item.q_type == 'MATCH' else "single"
        b_type = "directedPair" if item.q_type == 'MATCH' else "identifier"
        rd = etree.SubElement(root, "responseDeclaration", identifier="RESPONSE", cardinality=card, baseType=b_type)
        cr = etree.SubElement(rd, "correctResponse")

        if item.q_type == 'MATCH':
            for src in item.match_sources:
                if src.target_id:
                    etree.SubElement(cr, "value").text = f"{src.id} {src.target_id}"
        elif item.q_type == 'MCQ':
            cid = "c0"
            for i, c in enumerate(item.choices):
                if c['correct'] or (item.solution_label and c['label'] == item.solution_label): cid = f"c{i}"; break
            etree.SubElement(cr, "value").text = cid

        # 4. Body
        body = etree.SubElement(root, "itemBody")
        etree.SubElement(body, "div").text = item.prompt
        if item.solution: etree.SubElement(body, "div", attrib={"class": "solution"}).text = f"{item.solution}"

        if item.q_type == 'MATCH':
            shuf_attr = "true" if item.shuffle else "false"
            inter = etree.SubElement(body, "matchInteraction", responseIdentifier="RESPONSE", shuffle=shuf_attr, maxAssociations=str(len(item.match_sources)))
            src_set = etree.SubElement(inter, "simpleMatchSet")
            for src in item.match_sources: etree.SubElement(src_set, "simpleAssociableChoice", identifier=src.id, matchMax="1").text = src.text
            tgt_set = etree.SubElement(inter, "simpleMatchSet")
            for cat in item.match_targets: etree.SubElement(tgt_set, "simpleAssociableChoice", identifier=cat['id'], matchMax=str(len(item.match_sources))).text = cat['text']
        elif item.q_type == 'MCQ':
            shuf = "true" if item.shuffle else "false"
            inter = etree.SubElement(body, "choiceInteraction", responseIdentifier="RESPONSE", shuffle=shuf, maxChoices="1")
            for i, c in enumerate(item.choices): etree.SubElement(inter, "simpleChoice", identifier=f"c{i}").text = c['text']

        with open(folder / filename, 'wb') as f:
            f.write(etree.tostring(root, pretty_print=True, xml_declaration=True, encoding='UTF-8'))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    if args.input.exists():
        with open(args.input, 'r', encoding='utf-8') as f: lines = f.readlines()
        ast = IQBParser(lines).run()
        QTI3FinalBuilder().build(ast, args.input)
