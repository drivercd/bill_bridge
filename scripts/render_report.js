const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({args:['--no-sandbox']});
  const p = await b.newPage();
  await p.goto('file://' + process.argv[2], {waitUntil: 'networkidle'});
  await p.pdf({path: process.argv[3], format: 'Letter', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:7pt;color:#6b7280;width:100%;text-align:center;font-family:Helvetica">M3 Team 3 BillBridge · page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'});
  await b.close();
})();
