# Copyright 2017 Deborah Kaplan
#
# This file is part of Abbyy-to-epub3.
# Source code is available at <https://github.com/deborahgu/abbyy-to-epub3>.
#
# Abbyy-to-epub3 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from collections import OrderedDict
from ebooklib import epub
from ebooklib import utils as ebooklib_utils
from fuzzywuzzy import fuzz
from numeral import roman2int
from PIL import Image
from pkg_resources import resource_filename

from zipfile import ZipFile

import configparser
import gzip
import logging
import os
import re
import tempfile

from abbyy_to_epub3.constants import skippable_pages
from abbyy_to_epub3.parse_abbyy import AbbyyParser
from abbyy_to_epub3.parse_scandata import ScandataParser
from abbyy_to_epub3.utils import dirtify_xml, is_increasing
from abbyy_to_epub3.verify_epub import EpubVerify


# Set up configuration
config = configparser.ConfigParser()
configfile = resource_filename("abbyy_to_epub3", "config.ini")
config.read(configfile)


class Ebook(object):
    """
    The Ebook object.

    Holds extracted information about a book & the ebooklib EPUB object.
    """
    base = ''         # the book's identifier, used in many filename
    metadata = {}     # the book's metadata
    blocks = []       # each text or non-text block, with contents & attributes
    paragraphs = {}   # paragraph style info
    tmpdir = ''       # stores converted images and extracted zip files
    abbyy_file = ''   # the ABBYY XML file
    cover_img = ''    # the name of the cover image
    chapters = []     # holds each of the chapter (EpubHtml) objects
    progression = ''  # page direction
    firsts = {}       # all first lines per-page
    lasts = {}        # all last lines per-page
    pages = dict()    # page-by-page information from scandata
    # are there headers, footers, or page numbers?
    headers_present = False
    pagenums_found = False
    rpagenums_found = False
    table = False
    table_row = False
    table_cell = False

    book = epub.EpubBook()  # the book itself

    def __init__(self, base, debug=False, args=False):
        self.logger = logging.getLogger(__name__)
        if debug:
            self.logger.addHandler(logging.StreamHandler())
            self.logger.setLevel(logging.DEBUG)

        self.debug = debug
        self.args = args
        self.base = base
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cover_img = '{}/cover.png'.format(self.tmpdir.name)
        self.abbyy_file = "{tmp}/{base}_abbyy".format(
            tmp=self.tmpdir.name, base=self.base
        )
        self.logger.debug("Temp directory: {}\nidentifier: {}".format(
            self.tmpdir.name, self.base))

    def load_scandata_pages(self):
        """
        Parse the page-by-page scandata file. This stores page size,
        right or left leaf, and page type (eg copyright, color card, etc).
        """
        self.scandata = "{base}/{base}_scandata.xml".format(base=self.base)

        # parse the scandata
        parser = ScandataParser(
            self.scandata,
            self.pages,
            debug=self.debug,
        )
        parser.parse_scandata()

    def create_accessibility_metadata(self):
        """ Set up accessibility metadata """
        ALT_TEXT_PRESENT = config.getboolean('Main', 'ALT_TEXT_PRESENT')
        IMAGES_PRESENT = config.getboolean('Main', 'IMAGES_PRESENT')
        OCR_GENERATED = config.getboolean('Main', 'OCR_GENERATED')
        TEXT_PRESENT = config.getboolean('Main', 'TEXT_PRESENT')

        summary = ''
        modes = []
        modes_sufficient = []
        features = ['printPageNumbers', 'tableOfContents', ]

        if OCR_GENERATED:
            summary += (
                'The publication was generated using automated character '
                'recognition, therefore it may not be an accurate rendition '
                'of the original text, and it may not offer the correct '
                'reading sequence.'
            )
        if IMAGES_PRESENT:
            modes.append('visual')
            if ALT_TEXT_PRESENT:
                features.append('alternativeText')
            else:
                summary += (
                    'This publication is missing meaningful alternative text.'
                )
        if TEXT_PRESENT:
            modes.append('textual')
            if IMAGES_PRESENT:
                modes_sufficient.append('textual,visual')
                if ALT_TEXT_PRESENT:
                    modes_sufficient.append('textual')
            else:
                modes_sufficient.append('textual')
        elif IMAGES_PRESENT and ALT_TEXT_PRESENT:
            modes_sufficient.append('textual,visual')
            modes_sufficient.append('visual')
        elif IMAGES_PRESENT:
            modes_sufficient.append('visual')
        if OCR_GENERATED:
            # these states will be true for any static content,  which we know
            # is guaranteed for OCR generated texts.
            hazards = [
                'noFlashingHazard',
                'noMotionSimulationHazard',
                'noSoundHazard',
            ]
            controls = [
                'fullKeyboardControl',
                'fullMouseControl',
                'fullSwitchControl',
                'fullTouchControl',
                'fullVoiceControl',
            ]

        if summary:
            summary += 'The publication otherwise meets WCAG 2.0 Level A.'
        else:
            summary = 'The publication meets WCAG 2.0 Level A.'

        # Add the metadata to the publication
        self.book.add_metadata(
            None,
            'meta',
            summary,
            OrderedDict([('property', 'schema:accessibilitySummary')])
        )
        for feature in features:
            self.book.add_metadata(
                None,
                'meta',
                feature,
                OrderedDict([('property', 'schema:accessibilityFeature')])
            )
        for mode in modes:
            self.book.add_metadata(
                None,
                'meta',
                mode,
                OrderedDict([('property', 'schema:accessMode')])
            )
        for mode_sufficient in modes_sufficient:
            self.book.add_metadata(
                None,
                'meta',
                mode_sufficient,
                OrderedDict([('property', 'schema:accessModeSufficient')])
            )
        if hazards:
            for hazard in hazards:
                self.book.add_metadata(
                    None,
                    'meta',
                    hazard,
                    OrderedDict([('property', 'schema:accessibilityHazard')])
                )
        if controls:
            for control in controls:
                self.book.add_metadata(
                    None,
                    'meta',
                    control,
                    OrderedDict([('property', 'schema:accessibilityControl')])
                )

    def extract_images(self):
        """
        Extracts all of the images for the text.

        For efficiency's sake, do these all at once. Memory & CPU will be at a
        higher premium than disk space, so unzip the entire scan file into temp
        directory, instead of extracting only the needed images.
        """
        images_zipped = "{base}/{base}_jp2.zip".format(base=self.base)
        cover_file = "{tmp}/{base}_jp2/{base}_0001.jp2".format(
            tmp=self.tmpdir.name, base=self.base
        )
        with ZipFile(images_zipped) as f:
            f.extractall(self.tmpdir.name)

        # convert the JP2K file into a PNG for the cover
        f, e = os.path.splitext(os.path.basename(cover_file))
        try:
            Image.open(cover_file).save(self.cover_img)
        except IOError as e:
            self.logger.warning("Cannot create cover file: {}".format(e))

    def image_dim(self, block):
        """
        Given a dict object containing the block info for an image, generate
        a tuple of its dimensions:
        (left, top, right, bottom)
        """
        left = int(block['style']['l'])
        top = int(block['style']['t'])
        right = int(block['style']['r'])
        bottom = int(block['style']['b'])

        return (left, top, right, bottom)

    def make_image(self, block):
        """
        Given a dict object containing the block info for an image, generate
        the image HTML
        """
        page_no = block['page_no']
        if page_no == 1:
            # The first page's image is made into the cover automatically
            return

        # pad out the filename to four digits
        origfile = '{dir}/{base}_jp2/{base}_{page:0>4}.jp2'.format(
            dir=self.tmpdir.name,
            base=self.base,
            page=block['page_no']
        )
        basefile = 'img_{:0>4}.png'.format(self.picnum)
        pngfile = '{}/{}'.format(self.tmpdir.name, basefile)
        in_epub_imagefile = 'images/{}'.format(basefile)

        # get image dimensions from ABBYY block attributes
        # (left, top, right, bottom)
        box = self.image_dim(block)
        width = box[2] - box[0]
        height = box[3] - box[1]

        # ignore if this image is entirely encapsulated in another image
        for each_pic in self.metadata['pics_by_page']:
            # Ignore if this is just the block itself
            if each_pic == block:
                continue
            new_box = self.image_dim(each_pic)
            for (old, new) in zip(box, new_box):
                if old <= new:
                    return

        # make the image:
        try:
            i = Image.open(origfile)
        except IOError as e:
            self.logger.warning("Can't open image {}: {}".format(origfile, e))
        try:
            i.crop(box).save(pngfile)
        except IOError as e:
            self.logger.warning("Can't crop image {} & save to {}: {}".format(
                origfile, pngfile, e
            ))
        epubimage = epub.EpubImage()
        epubimage.file_name = in_epub_imagefile
        with open(pngfile, 'rb') as f:
            epubimage.content = f.read()
        epubimage = self.book.add_item(epubimage)

        container_w = width / int(block['style']['pagewidth']) * 100
        content = u'''
        <div style="width: {c_w}%;">
        <img src="{src}" alt="Picture #{picnum}">
        </div>
        '''.format(
            c_w=container_w,
            src=in_epub_imagefile,
            picnum=self.picnum,
            w=width,
            h=height,)

        # increment the image number
        self.picnum += 1

        return content

    def make_chapter(self, heading, chapter_no):
        """
        Create a chapter section in an ebooklib.epub.
        """
        if not heading:
            heading = "Chapter {}".format(chapter_no)

        # The epub library escapes the XML itself
        chapter = epub.EpubHtml(
            title=dirtify_xml(heading).replace("\n", " "),
            direction=self.progression,
            # pad out the filename to four digits
            file_name='chap_{:0>4}.xhtml'.format(chapter_no),
            lang='{}'.format(self.metadata['language'][0])
        )
        chapter.content = u''
        chapter.add_link(
            href='style/style.css', rel='stylesheet', type='text/css'
        )
        self.chapters.append(chapter)
        self.book.add_item(chapter)

        return chapter

    def identify_headers_footers_pagenos(self, placement):
        """
        Attempts to identify the presence of headers, footers, or page numbers

        1. Build a dict of first & last lines, indexed by page number.
        2. Try to identify headers and footers.

        Headers and footers can appear on every page, or on alternating pages
        (for example if one page has header of the title, the facing page
        might have the header of the chapter name).

        They may include a page number, or the page number might be a
        standalone header or footer.

        The presence of headers and footers in the document does not mean they
        appear on every page (for example, chapter openings or illustrated
        pages sometimes don't contain the header/footer, or contain a modified
        version, such as a standalone page number).

        Page numbers may be in Arabic or Roman numerals.

        This method does not attempt to look for all edge cases. For example,
        it will not find:
        - constantly varied headers, as in a dictionary
        - page numbers that don't steadily increase
        - page numbers misidentified in the OCR process, eg. IO2 for 102
        - page numbers with characters around them, eg. '~ 45 ~'
        """

        # running this on first lines or last lines?
        if placement == 'first':
            mylines = self.firsts
        else:
            mylines = self.lasts
        self.logger.debug("Looking for headers/footers: {}".format(placement))

        # Look for standalone strings of digits
        digits = re.compile(r'^\d+$')
        romans = re.compile(r'^[xicmlvd]+$')
        candidate_digits = []
        candidate_romans = []
        for block in self.blocks:
            if placement in block:
                line = block['text']
                ourpageno = block['page_no']
                mylines[ourpageno] = {'text': block['text']}
                pageno = digits.search(line)
                rpageno = romans.search(line, re.IGNORECASE)
                if rpageno:
                    # Is this a roman numeral?
                    try:
                        # The numeral.roman2int method is very permissive
                        # for archaic numeral forms, which is good.
                        num = roman2int(line)
                    except ValueError:
                        # not a roman numeral
                        pass
                    mylines[ourpageno]['ocr_roman'] = placement
                    candidate_romans.append(num)
                elif pageno:
                    mylines[ourpageno]['ocr_digits'] = placement
                    candidate_digits.append(int(line))

        # The algorithms to find false positives in page number candidates
        # are resource intensive, so this excludes anything where the candidate
        # numbers aren't monotonically increasing.
        if candidate_digits and is_increasing(candidate_digits):
            self.pagenums_found = True
            self.logger.debug("Page #s found: {}".format(candidate_digits))
        if candidate_romans and is_increasing(candidate_romans):
            self.rpagenums_found = True
            self.logger.debug("Roman #s found: {}".format(candidate_romans))

        # identify match ratio
        fuzz_consecutive = 0
        fuzz_alternating = 0
        for k, v in mylines.items():
            # Check to see if there's still one page forward
            if k + 1 in mylines:
                ratio_consecutive = fuzz.ratio(
                    v['text'],
                    mylines[k + 1]['text']
                )
                mylines[k]['ratio_consecutive'] = ratio_consecutive
                fuzz_consecutive += ratio_consecutive
            # Check to see if there's still two pages forward
            if k + 2 in mylines:
                ratio_alternating = fuzz.ratio(
                    v['text'],
                    mylines[k + 2]['text']
                )
                mylines[k]['ratio_alternating'] = ratio_alternating
                fuzz_alternating += ratio_alternating

        # occasional similar first/last lines might happen in all texts,
        # so only identify headers & footers if there are many of them
        HEADERS_PRESENT_THRESHOLD = int(
            config.get('Main', 'HEADERS_PRESENT_THRESHOLD')
        )
        if len(mylines) > 2:
            average_consecutive = fuzz_consecutive / (len(mylines) - 1)
            average_alternating = fuzz_alternating / (len(mylines) - 2)
            self.logger.debug("{}: consecutive fuzz avg.: {}".format(
                placement,
                average_consecutive
            ))
            self.logger.debug("{}: alternating fuzz avg.: {}".format(
                placement,
                average_alternating
            ))
            if average_consecutive > HEADERS_PRESENT_THRESHOLD:
                if placement == 'first':
                    self.headers_present = 'consecutive'
                else:
                    self.footers_present = 'consecutive'
                self.logger.debug(
                    "{} repeated, consecutive pages".format(placement)
                )
            elif average_alternating > HEADERS_PRESENT_THRESHOLD:
                if placement == 'first':
                    self.headers_present = 'alternating'
                else:
                    self.footers_present = 'alternating'
                self.logger.debug(
                    "{} repeated, alternating pages".format(placement)
                )

    def is_header_footer(self, block, placement):
        """
        Given a block and our identified text structure, return True if this
        block's text is a header, footer, or page number to be ignored, False
        otherwise.
        """
        THRESHOLD = int(
            config.get('Main', 'FUZZY_HEADER_THRESHOLD')
        )

        # running this on first lines or last lines?
        if placement == 'first':
            mylines = self.firsts
        else:
            mylines = self.lasts

        ourpageno = block['page_no']
        if ourpageno in mylines:
            if (
                self.rpagenums_found and
                'ocr_roman' in mylines[ourpageno] and
                mylines[ourpageno]['ocr_roman'] == placement
            ):
                # This is an identified roman numeral page number
                return True
            if (
                self.pagenums_found and
                'ocr_digits' in mylines[ourpageno] and
                mylines[ourpageno]['ocr_digits'] == placement
            ):
                # This is an identified page number
                self.logger.debug(
                    "identified page number: {}".format(block['text'])
                )
                return True
            if (
                self.headers_present == 'consecutive' and
                'ratio_consecutive' in mylines[ourpageno] and
                mylines[ourpageno]['ratio_consecutive'] >= THRESHOLD
            ):
                return True
            if (
                self.headers_present == 'alternating' and
                'ratio_alternating' in mylines[ourpageno] and
                mylines[ourpageno]['ratio_alternating'] >= THRESHOLD
            ):
                return True
        return False

    def set_metadata(self):
        """
        Set the metadata on the epub object
        """
        self.book.set_identifier(self.metadata['identifier'][0])
        for language in self.metadata['language']:
            self.book.set_language(language)
        for title in self.metadata['title']:
            self.book.set_title(title)
        if 'creator' in self.metadata:
            for creator in self.metadata['creator']:
                self.book.add_author(creator)
        if 'description' in self.metadata:
            for description in self.metadata['description']:
                self.book.add_metadata('DC', 'description', description)
        if 'publisher' in self.metadata:
            for publisher in self.metadata['publisher']:
                self.book.add_metadata('DC', 'publisher', publisher)
        if 'identifier-access' in self.metadata:
            for identifier_access in self.metadata['identifier-access']:
                self.book.add_metadata(
                    'DC', 'identifier', 'Access URL: {}'.format(
                        identifier_access
                    )
                )
        if 'identifier-ark' in self.metadata:
            for identifier_ark in self.metadata['identifier-ark']:
                self.book.add_metadata(
                    'DC', 'identifier', 'urn:ark:{}'.format(identifier_ark)
                )
        if 'isbn' in self.metadata:
            for isbn in self.metadata['isbn']:
                self.book.add_metadata(
                    'DC', 'identifier', 'urn:isbn:{}'.format(isbn)
                )
        if 'oclc-id' in self.metadata:
            for oclc_id in self.metadata['oclc-id']:
                self.book.add_metadata(
                    'DC', 'identifier', 'urn:oclc:{}'.format(oclc_id)
                )
        if 'external-identifier' in self.metadata:
            for external_identifier in self.metadata['external-identifier']:
                self.book.add_metadata('DC', 'identifier', external_identifier)
        if 'related-external-id' in self.metadata:
            for related_external_id in self.metadata['related-external-id']:
                self.book.add_metadata('DC', 'identifier', related_external_id)
        if 'subject' in self.metadata:
            for subject in self.metadata['subject']:
                self.book.add_metadata('DC', 'subject', subject)
        if 'date' in self.metadata:
            for date in self.metadata['date']:
                self.book.add_metadata('DC', 'date', date)

    def craft_html(self):
        """
        Assembles the XHTML content.

        Create some minimal navigation:
        * Break sections at text elements marked role: heading
        * Break files at any headings with roleLevel: 1
        Imperfect, but better than having no navigation or monster files.

        Images will get alternative text of "Picture #" followed by an index
        number for this image. Barring real alternative text for
        true accessibility, this at least adds some identifying information.
        """

        # Default section to hold cover image plus all until the 1st heading
        if 'title' in self.metadata:
            heading = self.metadata['title'][0]
        else:
            heading = "Opening Section"
        chapter_no = 1
        self.picnum = 1
        blocks_index = -1
        self.last_row = False

        # Look for headers and page numbers
        # FR10 has markup but isn't reliable so look there as well
        self.identify_headers_footers_pagenos('first')
        self.identify_headers_footers_pagenos('last')
        self.last_row = False
        self.last_cell = False

        # Make the initial chapter stub
        chapter = self.make_chapter(heading, chapter_no)
        endnotes = '<ul>'
        noteref = 1

        # Make a title page
        chapter.content += u'<h1 dir="ltr" class="center">{}</h1>'.format(
            heading
        )
        if 'title-alt-script' in self.metadata:
            for i in self.metadata['title-alt-script']:
                chapter.content += (
                    u'<p dir="auto" class="center bold big">{}</p>'
                ).format(i)
        if 'creator' in self.metadata:
            for i in self.metadata['creator']:
                chapter.content += (
                    u'<p dir="ltr" class="center bold">{}</p>'
                ).format(i)
        if 'creator-alt-script' in self.metadata:
            for i in self.metadata['creator-alt-script']:
                chapter.content += (
                    u'<p dir="auto" class="center bold">{}</p>'
                ).format(i)
        chapter.content += (
            '<div class="offset">'
            '<p dir="ltr">This book was produced in EPUB format by the '
            'Internet Archive.</p> '
            '<p dir="ltr">The book pages were scanned and converted to EPUB '
            'format automatically. This process relies on optical character '
            'recognition, and is somewhat susceptible to errors. The book may '
            'not offer the correct reading sequence, and there may be '
            'weird characters, non-words, and incorrect guesses at '
            'structure. Some page numbers and headers or footers may remain '
            'from the scanned page. The process which identifies images might '
            'have found stray marks on the page which are not actually images '
            'from the book. The hidden page numbering which may be available '
            'to your ereader corresponds to the numbered pages in the print '
            'edition, but is not an exact match;  page numbers will increment '
            'at the same rate as the corresponding print edition, but we may '
            'have started numbering before the print book\'s visible page '
            'numbers.  The Internet Archive is working to improve the '
            'scanning process and resulting books, but in the meantime, we '
            'hope that this book will be useful to you.</p> '
            '<p dir="ltr">The Internet Archive was founded in 1996 to build '
            'an Internet library and to promote universal access to all '
            'knowledge. The Archive\'s purposes include offering permanent '
            'access for researchers, historians, scholars, people with '
            'disabilities, and ' 'the general public to historical '
            'collections that exist in digital format. The Internet Archive '
            'includes texts, audio, moving images, '
            'and software as well as archived web pages, and provides '
            'specialized services for information access for the blind and '
            'other persons with disabilities.</p></div>'
        )

        for block in self.blocks:
            blocks_index += 1

            if 'type' not in block:
                continue
            if (
                'page_no' in block and
                block['page_no'] in self.pages and
                self.pages[block['page_no']] in skippable_pages
            ):
                continue
            if (
                'style' in block and
                'fontstyle' in block['style']
            ):
                fclass = ''
                fontstyle = block['style']['fontstyle']
                fsize = fontstyle['fs']
                if 'italic' in fontstyle:
                    fclass += 'italic '
                if 'bold' in fontstyle:
                    fclass += 'bold '
                if 'Serif' in fontstyle['ff'] or 'Times' in fontstyle['ff']:
                    fclass += 'serif '
                elif 'Sans' in fontstyle['ff']:
                    fclass += 'sans '

                fstyling = (
                    'class="{fclass}" style="font-size: {fsize}pt"'
                ).format(
                    fclass=fclass,
                    fsize=fsize,
                )
            else:
                fstyling = ''
            if block['type'] == 'Text':
                text = block['text']
                role = block['role']

                # This is the first text element on the page
                if 'first' in block:
                    # Look for headers and page numbers
                    if self.is_header_footer(block, 'first'):
                        self.logger.debug("Stripping header {}".format(text))
                        continue

                # Look for footers and page numbers
                if (
                    'last' in block and
                    self.is_header_footer(block, 'last')
                ):
                        self.logger.debug("Stripping footer {}".format(text))
                        continue
                if role == 'footnote':
                    # Footnote. Our ABBYY markup doesn't indicate references,
                    # so fake them, right above the bottom of the page so
                    # they'll be reachable by all adaptive tech & user agents.
                    # Place as endnotes to improve cross-ereader reachability.

                    chapter.content += (
                        u'<p class="small">'
                        u'<a epub:type="noteref" href="#n{page}_{ref}">'
                        u'Note {ref}</a></p>'
                    ).format(
                        page=block['page_no'],
                        ref=noteref,
                    )
                    # must use now deprecated "rearnote" instead of "endnote"
                    # for now; endnote support is limited. Change when more
                    # readers support endnote.
                    endnotes += (
                        u'<li><aside epub:type="rearnote" id="n{page}_{ref}">'
                        u'{text}</aside></li>'
                    ).format(
                        page=block['page_no'],
                        ref=noteref,
                        text=text,
                    )
                    noteref += 1
                elif role == 'tableCaption':
                    # It would be ideal to mark up table captions as <caption>
                    # within the associated table.  However, the ABBYY markup
                    # doesn't have a way to associate the caption with the
                    # specific table, and there's no way of knowing if the
                    # caption is for a table immediately following or
                    # immediately prior. Add a little styling to make it more
                    # obvious, and some accessibility helpers.
                    chapter.content += (
                        u'<p {style}><span class="sr-only">'
                        u'Table caption</span>{text}</p>'
                    ).format(
                        style=fstyling,
                        text=text,
                    )
                elif role == 'heading':
                    if int(block['heading']) > 1:
                        # Heading >1. Format as heading
                        # but don't make new chapter.
                        chapter.content += u'<h{lev}>{text}</h{lev}>'.format(
                            lev=block['heading'], text=text
                        )
                    else:
                        # attach any endnotes to the chapter.
                        if noteref > 1:
                            chapter.content += '<hr /><h2>Chapter Notes</h2>'
                            chapter.content += endnotes
                            chapter.content += '</ol>'
                            noteref = 1
                            endnotes = '<ol>'

                        # Heading 1. Begin the new chapter
                        chapter_no += 1
                        chapter = self.make_chapter(text, chapter_no)
                        chapter.content = u'<h{lev}>{text}</h{lev}>'.format(
                            lev=block['heading'], text=text
                        )
                else:
                    # Regular or other text block. Add its heading to the
                    # chapter content. In theory a table of contents could get
                    # parsed for page numbers and turned into a hyperlinked
                    # nav toc pointing to page elements, but relying on headers
                    # is probably more reliable.
                    chapter.content += u'<p {style}>{text}</p>'.format(
                        style=fstyling,
                        text=text,
                    )
            elif block['type'] == 'Page':
                # nest this conditional; we don't want to short circuit if no
                # pages_support
                if self.metadata['PAGES_SUPPORT']:
                    chapter.content += ebooklib_utils.create_pagebreak(
                        str(block['text'])
                    )
            elif block['type'] == 'Picture':
                # Image
                content = self.make_image(block)
                if content:
                    chapter.content += content
            elif (
                block['type'] == 'Separator' or
                block['type'] == 'SeparatorsBox'
            ):
                # Separator blocks seem to be fairly randomly applied and don't
                # correspond to anything useful in the original content
                pass
            elif block['type'] == 'Table':
                chapter.content += u'<table>'
            elif block['type'] == 'TableRow':
                chapter.content += u'<tr>'
                if 'last_table_elem' in block:
                    self.last_row = True
            elif block['type'] == 'TableCell':
                chapter.content += u'<td>'
                if 'last_table_elem' in block:
                    self.last_cell = True
            elif block['type'] == 'TableText':
                chapter.content += u'<p {style}>{text}</p>'.format(
                    style=fstyling,
                    text=block['text'],
                )
                if 'last_table_elem' in block:
                    chapter.content += u'</td>'
                    if self.last_cell:
                        chapter.content + u'</tr>'
                        self.last_cell = False
                        if self.last_row:
                            chapter.content += u'</table>'
                            self.last_row = False
            else:
                self.logger.debug(
                    "Ignoring Block:\n Type: {}\n Attribs: {}".format(
                        block['type'], block['style']
                    )
                )

    def craft_epub(self):
        """ Assemble the extracted metadata & text into an EPUB  """

        # document files and directories
        abbyy_file_zipped = "{base}/{base}_abbyy.gz".format(base=self.base)
        metadata_file = "{base}/{base}_meta.xml".format(base=self.base)

        # Unzip the ABBYY file to disk. (Might be too huge to hold in memory.)
        with gzip.open(abbyy_file_zipped, 'rb') as infile:
            with open(self.abbyy_file, 'wb') as outfile:
                for line in infile:
                    outfile.write(line)

        # Extract the page images and create the cover file
        self.extract_images()

        # read in the page-by-page scandata file
        self.load_scandata_pages()

        # parse the ABBYY
        parser = AbbyyParser(
            self.abbyy_file,
            metadata_file,
            self.metadata,
            self.paragraphs,
            self.blocks,
            debug=self.debug,
        )
        parser.parse_abbyy()
        
        # Text direction: convert IA abbreviation to epub abbreviation
        direction = {
            'lr': 'ltr',
            'rl': 'rtl',
        }
        if 'page-progression' in self.metadata:
            self.progression = direction[self.metadata['page-progression'][0]]
        else:
            self.progression = 'default'
        self.book.set_direction(self.progression)

        # get the finereader version
        if 'fr-version' in self.metadata:
            self.version = self.metadata['fr-version']

        # make the HTML chapters
        self.craft_html()

        # Set the book's cover
        self.book.set_cover(
            'images/cover.png',
            open(self.cover_img, 'rb').read()
        )
        cover = self.book.items[-1]
        cover.add_link(
            href='style/style.css', rel='stylesheet', type='text/css'
        )

        # Set the book's metadata
        self.set_metadata()

        # set the accessibility metadata
        self.create_accessibility_metadata()

        # Navigation for EPUB 3 & EPUB 2 fallback
        self.book.toc = self.chapters
        self.book.add_item(epub.EpubNcx())
        self.book.add_item(epub.EpubNav())
        # cover_ncx hack to work around Adobe Digital Editions problem
        self.book.spine = ['cover', 'nav', ] + self.chapters

        # define CSS style
        style = """.center {text-align: center}
                .sr-only {
                    position: absolute;
                    width: 1px;
                    height: 1px;
                    padding: 0;
                    margin: -1px;
                    overflow: hidden;
                    clip: rect(0,0,0,0);
                    border: 0;
                }
                .strong {font-weight: bold;}
                .italic {font-style: italic;}
                .serif {font-family: serif;}
                .sans {font-family: sans-serif;}
                .big {font-size: 1.5em;}
                .small {font-size: .75em;}
                .offset {
                    margin: 1em;
                    padding: 1.5em;
                    border: black 1px solid;
                }
                img {
                    padding: 0;
                    margin: 0;
                    max-width: 100%;
                    max-height: 100%;
                    column-count: 1;
                    break-inside: avoid;
                    oeb-column-number: 1;
                }
                """
        css_file = epub.EpubItem(
            uid="style_nav",
            file_name="style/style.css",
            media_type="text/css",
            content=style
        )
        self.book.add_item(css_file)

        epub_filename = '{base}/{base}.epub'.format(base=self.base)
        epub.write_epub(epub_filename, self.book, {})

        # run checks
        verifier = EpubVerify(self.debug)
        if self.args.epubcheck:
            self.logger.info("Running EpubCheck on {}".format(epub_filename))
            verifier.run_epubcheck(epub_filename)
        if self.args.ace:
            self.logger.info("Running DAISY Ace on {}".format(epub_filename))
            verifier.run_ace(epub_filename)

        # Clean up. ebooklib.epub doesn't clean up cleanly without reset,
        # causing problems on consecutive runs
        self.tmpdir.cleanup()
        self.book.reset()
