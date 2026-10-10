# Online Merchants — Task 1


This task asks the agent to act as the E-commerce Operations Manager for an online fashion boutique, investigating whether three escalated complaints (INQ-001, INQ-003, INQ-006) reflect systemic supplier quality issues, fulfillment errors, or listing-language mismatches, then producing an incident report and the appropriate supplier communication.

---

## 1. Capability Professionals Want Agents to Handle

**ONET Task Summary:** *Investigate products or markets to determine areas for opportunity or viability for merchandising specific products, using online or offline sources.*

**Expert Reported Automation Desire:** 4.00 (scale: 0–5, from WorkBank)

In practice this means cross-referencing escalated complaints against Q4 product performance metrics (return rate, rating), pulling industry context from external review datasets, examining whether listing copy ("ripped hem effect") is driving expectation mismatches, and producing an evidence-backed verdict for each SKU. The investigation is the explicit deliverable.

**Why desired:** E-commerce ops managers spend hours pivoting return-rate tables, manually combing review sites for pattern context, and drafting supplier letters from scratch. An agent that handles the cross-referencing layer lets the merchant focus on the verdict itself — is this a supplier issue, a warehouse issue, or copy that needs rewriting — and on the supplier negotiation that follows.

---

## 2. Reasoning Challenges by Design

- **INQ-001 is shipping, not quality**
  Despite high-priority flag, INQ-001 (Yellow Wool Jumper) is a "package marked delivered but not received" issue. SKU-003 has a 4.5 rating and 3.8% return rate. The agent must avoid conflating delivery problems with product quality.

- **INQ-006 is fulfillment error, not quality**
  Customer received a Zipped Jacket Medium instead of a Navy Sports Jacket Large — a warehouse pick error. SKU-008 metrics are healthy (4.4 rating, 3.2% return rate). The agent must classify as process correction, not supplier action.

- **INQ-003 is the genuine systemic case**
  Dark Denim Top (SKU-007) carries the catalog's highest return rate (8.2%) and lowest rating (3.5), confirming the complaint reflects a real quality pattern requiring supplier action.

- **Listing copy ambiguity ("ripped hem effect")**
  The Dark Denim Top listing markets a "ripped hem effect" while the customer reports "tear near the hem." The agent must judge whether to treat as design-language confusion (listing fix) or actual defect (supplier action), informing the verdict for INQ-003.

- **Proactive at-risk product identification**
  Beyond the three escalated SKUs, Olive Green Jacket (SKU-016, 7.1% return, 3.7 rating, separate complaint INQ-011 about color) and Striped Silk Blouse (SKU-005, 6.3% return, 3.9 rating) cross the 7% apparel return threshold and belong on the Product Risk Dashboard.

- **Supplier notice quantitative standards**
  A defensible Supplier Quality Notice must cite specific defect patterns (not generic complaints), quantitative deviation from acceptable performance, and a corrective action request with timeline. The agent must produce all three rather than a vague letter.

---

## 3. Files Requiring Search

External datasets the agent must locate.

| File | Purpose | Source |
|---|---|---|
| `customer_reviews.csv` | Industry context on common fashion quality complaint patterns (fabric, sizing, construction defects) for benchmarking the boutique's complaints. | [github.com/AFAgarap/ecommerce-reviews-analysis](https://github.com/AFAgarap/ecommerce-reviews-analysis/raw/master/Womens%20Clothing%20E-Commerce%20Reviews.csv) |
| `shopify_apparel_products.csv` | Sample apparel product listings showing description language ("distressed", "ripped hem effect") that may be driving customer expectation mismatches. | [github.com/shopifypartners/product-csvs](https://github.com/shopifypartners/product-csvs/raw/master/apparel.csv) |
