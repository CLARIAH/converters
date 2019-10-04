#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import datetime
import json
import logging
import iribaker
import traceback
import rfc3987
from chardet.universaldetector import UniversalDetector
import multiprocessing as mp
import unicodecsv as csv
from jinja2 import Template
try:
    # Python 2
    from util import get_namespaces, Nanopublication, CSVW, PROV, DC, SKOS, RDF
except ImportError:
    from .util import get_namespaces, Nanopublication, CSVW, PROV, DC, SKOS, RDF
from rdflib import URIRef, Literal, Graph, BNode, XSD, Dataset
from rdflib.resource import Resource
from rdflib.collection import Collection
from functools import partial
try:
    # Python 3
    from itertools import zip_longest
except ImportError:
    # Python 2
    from itertools import izip_longest as zip_longest

import io

# Python 2 and 3 compatible unicode
# from builtins import str


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


# Serialization extension dictionary
extensions = {'xml': 'xml', 'n3' : 'n3', 'turtle': 'ttl', 'nt' : 'nt', 'pretty-xml' : 'xml', 'trix' : 'trix', 'trig' : 'trig', 'nquads' : 'nq'}


def build_schema(infile, outfile, delimiter=None, quotechar='\"', encoding=None, dataset_name=None, base="https://iisg.amsterdam/"):
    """
    Build a CSVW schema based on the ``infile`` CSV file, and write the resulting JSON CSVW schema to ``outfile``.

    Takes various optional parameters for instructing the CSV reader, but is also quite good at guessing the right values.
    """

    url = os.path.basename(infile)
    # Get the current date and time (UTC)
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    if dataset_name is None:
        dataset_name = url

    if encoding is None:
        detector = UniversalDetector()
        with open(infile, 'rb') as f:
            for line in f.readlines():
                detector.feed(line)
                if detector.done:
                    break
        detector.close()
        encoding = detector.result['encoding']
        logger.info("Detected encoding: {} ({} confidence)".format(detector.result['encoding'],
                                                                   detector.result['confidence']))

    if delimiter is None:
        with open(infile, 'r') as csvfile:
            # dialect = csv.Sniffer().sniff(csvfile.read(1024), delimiters=";,$\t")
            dialect = csv.Sniffer().sniff(csvfile.readline()) #read only the header instead of the entire file to determine delimiter
            csvfile.seek(0)
        logger.info("Detected dialect: {} (delimiter: '{}')".format(dialect, dialect.delimiter))
        delimiter = dialect.delimiter


    logger.info("Delimiter is: {}".format(delimiter))

    if base.endswith('/'):
        base = base[:-1]

    metadata = {
        u"@id": iribaker.to_iri(u"{}/{}".format(base, url)),
        u"@context": [u"https://raw.githubusercontent.com/CLARIAH/COW/master/csvw.json",
                     {u"@language": u"en",
                      u"@base": u"{}/".format(base)},
                     get_namespaces(base)],
        u"url": url,
        u"dialect": {u"delimiter": delimiter,
                    u"encoding": encoding,
                    u"quoteChar": quotechar
                    },
        u"dc:title": dataset_name,
        u"dcat:keyword": [],
        u"dc:publisher": {
            u"schema:name": u"CLARIAH Structured Data Hub - Datalegend",
            u"schema:url": {u"@id": u"http://datalegend.net"}
        },
        u"dc:license": {u"@id": u"http://opendefinition.org/licenses/cc-by/"},
        u"dc:modified": {u"@value": today, u"@type": u"xsd:date"},
        u"tableSchema": {
            u"columns": [],
            u"primaryKey": None,
            u"aboutUrl": u"{_row}"
        }
    }

    with io.open(infile, 'rb') as infile_file:
        r = csv.reader(infile_file, delimiter=delimiter, quotechar=quotechar)

        try:
            # Python 2
            header = r.next()
        except AttributeError:
            # Python 3
            header = next(r)

        logger.info(u"Found headers: {}".format(header))

        if u'' in header:
            logger.warning("WARNING: You have one or more empty column headers in your CSV file. Conversion might produce incorrect results because of conflated URIs or worse")
        if len(set(header)) < len(header):
            logger.warning("WARNING: You have two or more column headers that are syntactically the same. Conversion might produce incorrect results because of conflated URIs or worse")

        # First column is primary key
        metadata[u'tableSchema'][u'primaryKey'] = header[0]

        for head in header:
            col = {
                u"@id": iribaker.to_iri(u"{}/{}/column/{}".format(base, url, head)),
                u"name": head,
                u"titles": [head],
                u"dc:description": head,
                u"datatype": u"string"
            }

            metadata[u'tableSchema'][u'columns'].append(col)

    with open(outfile, 'w') as outfile_file:
        outfile_file.write(json.dumps(metadata, indent=True))

    logger.info("Done")
    return


class Item(Resource):
    """Wrapper for the rdflib.resource.Resource class that allows getting property values from resources."""

    def __getattr__(self, p):
        """Returns the object for predicate p, either as a list (when multiple bindings exist), as an Item
           when only one object exists, or Null if there are no values for this predicate"""
        try:
            objects = list(self.objects(self._to_ref(*p.split('_', 1))))
        except:
            # logger.debug("Calling parent function for Item.__getattr__ ...") #removed for readability
            super(Item, self).__getattr__(self, p)
            # raise Exception("Attribute {} does not specify namespace prefix/qname pair separated by an ".format(p) +
            #                 "underscore: e.g. `.csvw_tableSchema`")

        # If there is only one object, return it, otherwise return all objects.
        if len(objects) == 1:
            return objects[0]
        elif len(objects) == 0:
            return None
        else:
            return objects

    def _to_ref(self, pfx, name):
        """Concatenates the name with the expanded namespace prefix into a new URIRef"""
        return URIRef(self._graph.store.namespace(pfx) + name)


class CSVWConverter(object):
    """
    Converter configuration object for **CSVW**-style conversion. Is used to set parameters for a conversion,
    and to initiate an actual conversion process (implemented in :class:`BurstConverter`)

    Takes a dataset_description (in CSVW format) and prepares:

    * An array of dictionaries for the rows to pass to the :class:`BurstConverter` (either in one go, or in parallel)
    * A nanopublication structure for publishing the converted data (using :class:`converter.util.Nanopublication`)
    """

    def __init__(self, file_name, delimiter=',', quotechar='\"', encoding='utf-8', processes=4, chunksize=5000, output_format='nquads'):
        logger.info("Initializing converter for {}".format(file_name))
        self.file_name = file_name
        self.output_format = output_format
        self.target_file = self.file_name + '.' + extensions[self.output_format]
        schema_file_name = file_name + '-metadata.json'

        if not os.path.exists(schema_file_name) or not os.path.exists(file_name):
            raise Exception(
                "Could not find source or metadata file in path; make sure you called with a .csv file")

        self._processes = processes
        self._chunksize = chunksize
        logger.info("Processes: {}".format(self._processes))
        logger.info("Chunksize: {}".format(self._chunksize))

        self.np = Nanopublication(file_name)
        self.metadata = json.load(open(schema_file_name, 'r'))
        self.metadata_graph = Graph()
        with open(schema_file_name, 'rb') as f:
            try:
                self.metadata_graph.load(f, format='json-ld')
            except ValueError as err:
                err.message = err.message + " ; please check the syntax of your JSON-LD schema file"
                raise

        # # from pprint import pprint
        # # pprint([term for term in sorted(self.metadata_graph)])
        #
        # Get the URI of the schema specification by looking for the subject
        # with a csvw:url property.
        try:
            # Python 2
            (self.metadata_uri, _) = self.metadata_graph.subject_objects(CSVW.url).next()
        except AttributeError:
            # Python 3
            (self.metadata_uri, _) = next(self.metadata_graph.subject_objects(CSVW.url))

        self.metadata = Item(self.metadata_graph, self.metadata_uri)

        self.schema = self.metadata.csvw_tableSchema

        # Taking defaults from init arguments
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.encoding = encoding

        # Read csv-specific dialiect specification from JSON structure
        if self.metadata.csvw_dialect is not None:
            if self.metadata.csvw_dialect.csvw_delimiter is not None:
                self.delimiter = str(self.metadata.csvw_dialect.csvw_delimiter)

            if self.metadata.csvw_dialect.csvw_quotechar is not None:
                self.quotechar = str(self.metadata.csvw_dialect.csvw_quoteChar)

            if self.metadata.csvw_dialect.csvw_encoding is not None:
                self.encoding = str(self.metadata.csvw_dialect.csvw_encoding)

        logger.info("Quotechar: {}".format(self.quotechar.__repr__()))
        logger.info("Delimiter: {}".format(self.delimiter.__repr__()))
        logger.info("Encoding : {}".format(self.encoding.__repr__()))
        logger.warning(
            "Taking encoding, quotechar and delimiter specifications into account...")

        # The metadata schema overrides the default namespace values
        # (NB: this does not affect the predefined Namespace objects!)
        # DEPRECATED
        # namespaces.update({ns: url for ns, url in self.metadata['@context'][1].items() if not ns.startswith('@')})

        # Cast the CSVW column rdf:List into an RDF collection
        #print(self.schema.csvw_column)
        # print(len(self.metadata_graph))

        self.columns = Collection(self.metadata_graph, BNode(self.schema.csvw_column))
        # Python 3 can't work out Item so we'll just SPARQL the graph

        if not self.columns:
            self.columns = [o for s,p,o in self.metadata_graph.triples((None, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#first"), None))]
        #
        # from pprint import pprint
        # pprint(self.columns)
        # print("LOOOOOOOOOOOOOOOOOOOOOOO")
        # from pprint import pprint
        # # pprint(self.schema.csvw_column)
        # pprint([term for term in self.schema])
        # pprint('----------')
        # pprint([term for term in self.schema.csvw_column])

        #print(self.schema.csvw_column)


    def convert_info(self):
        """Converts the CSVW JSON file to valid RDF for serializing into the Nanopublication publication info graph."""

        results = self.metadata_graph.query("""SELECT ?s ?p ?o
                                               WHERE { ?s ?p ?o .
                                                       FILTER(?p = csvw:valueUrl ||
                                                              ?p = csvw:propertyUrl ||
                                                              ?p = csvw:aboutUrl)}""")

        for (s, p, o) in results:
            # Use iribaker
            try:
                # Python 2
                escaped_object = URIRef(iribaker.to_iri(unicode(o)))
            except NameError:
                # Python 3
                escaped_object = URIRef(iribaker.to_iri(str(o)))
                print(escaped_object)

            # If the escaped IRI of the object is different from the original,
            # update the graph.
            if escaped_object != o:
                self.metadata_graph.set((s, p, escaped_object))
                # Add the provenance of this operation.
                try:
                    # Python 2
                    self.np.pg.add((escaped_object,
                                PROV.wasDerivedFrom,
                                Literal(unicode(o), datatype=XSD.string)))
                except NameError:
                    # Python 3
                    self.np.pg.add((escaped_object,
                                PROV.wasDerivedFrom,
                                Literal(str(o), datatype=XSD.string)))
                    print(str(o))

        # Add the information of the schema file to the provenance graph of the
        # nanopublication

        # self.np.ingest(self.metadata_graph, self.np.pg.identifier)

        # for s,p,o in self.np.triples((None,None,None)):
        #     print(s.__repr__,p.__repr__,o.__repr__)

        return

    def convert(self):
        """Starts a conversion process (in parallel or as a single process) as defined in the arguments passed to the :class:`CSVWConverter` initialization"""
        logger.info("Starting conversion")

        # If the number of processes is set to 1, we start the 'simple' conversion (in a single thread)
        if self._processes == 1:
            self._simple()
        # Otherwise, we start the parallel processing procedure, but fall back to simple conversion
        # when it turns out that for some reason the parallel processing fails (this happens on some
        # files. The reason could not yet be determined.)
        elif self._processes > 1:
            try:
                self._parallel()
            except TypeError:
                logger.info(
                    "TypeError in multiprocessing... falling back to serial conversion")
                self._simple()
            except Exception:
                logger.error(
                    "Some exception occurred, falling back to serial conversion")
                traceback.print_exc()
                self._simple()
        else:
            logger.error("Incorrect process count specification")

    def _simple(self):
        """Starts a single process for converting the file"""
        with open(self.target_file, 'wb') as target_file:
            with open(self.file_name, 'rb') as csvfile:
                logger.info("Opening CSV file for reading")
                reader = csv.DictReader(csvfile,
                                        encoding=self.encoding,
                                        delimiter=self.delimiter,
                                        quotechar=self.quotechar)

                logger.info("Starting in a single process")
                c = BurstConverter(self.np.ag.identifier, self.file_name, self.metadata_uri, self.columns,
                                   self.schema, self.metadata_graph, self.encoding, self.output_format)
                # Out will contain an N-Quads serialized representation of the
                # converted CSV
                out = c.process(0, reader, 1)
                # We then write it to the file
                try:
                    # Python 2
                    target_file.write(out)
                except TypeError:
                    # Python 3
                    target_file.write(out.decode('utf-8'))

            # self.convert_info()
            # Finally, write the nanopublication info to file
            target_file.write(self.np.serialize(format=self.output_format))

    def _parallel(self):
        """Starts parallel processes for converting the file. Each process will receive max ``chunksize`` number of rows"""
        with open(self.target_file, 'wb') as target_file:
            with open(self.file_name, 'rb') as csvfile:
                logger.info("Opening CSV file for reading")
                reader = csv.DictReader(csvfile,
                                        encoding=self.encoding,
                                        delimiter=self.delimiter,
                                        quotechar=self.quotechar)

                # Initialize a pool of processes (default=4)
                pool = mp.Pool(processes=self._processes)
                logger.info("Running in {} processes".format(self._processes))

                # The _burstConvert function is partially instantiated, and will be successively called with
                # chunksize rows from the CSV file
                # print("LOOOOOOOOOOOOOOOOOOOOOOO")
                # from pprint import pprint
                # pprint([term.n3() for term in self.columns])
                burstConvert_partial = partial(_burstConvert,
                                               identifier=self.np.ag.identifier,
                                               file_name=self.file_name,
                                               metadata_uri=self.metadata_uri,
                                               columns=self.columns,
                                               schema=self.schema,
                                               metadata_graph=self.metadata_graph,
                                               encoding=self.encoding,
                                               chunksize=self._chunksize,
                                               output_format=self.output_format)

                # The result of each chunksize run will be written to the
                # target file
                for out in pool.imap(burstConvert_partial, enumerate(grouper(self._chunksize, reader))):
                    target_file.write(out)

                # Make sure to close and join the pool once finished.
                pool.close()
                pool.join()

            #  self.convert_info()
            # Finally, write the nanopublication info to file
            target_file.write(self.np.serialize(format=self.output_format))


def grouper(n, iterable, padvalue=None):
    "grouper(3, 'abcdefg', 'x') --> ('a','b','c'), ('d','e','f'), ('g','x','x')"
    return zip_longest(*[iter(iterable)] * n, fillvalue=padvalue)


# This has to be a global method for the parallelization to work.
def _burstConvert(enumerated_rows, identifier, file_name, metadata_uri, columns, schema, metadata_graph, encoding, chunksize, output_format):
    """The method used as partial for the parallel processing initiated in :func:`_parallel`."""
    try:
        count, rows = enumerated_rows
        c = BurstConverter(identifier, file_name, metadata_uri, columns, schema,
                           metadata_graph, encoding, output_format)

        logger.info("Process {}, nr {}, {} rows".format(
            mp.current_process().name, count, len(rows)))

        result = c.process(count, rows, chunksize)

        logger.info("Process {} done".format(mp.current_process().name))

        return result
    except:
        traceback.print_exc()


class BurstConverter(object):
    """The actual converter, that processes the chunk of lines from the CSV file, and uses the instructions from the ``schema`` graph to produce RDF."""

    def __init__(self, identifier, file_name, metadata_uri, columns, schema, metadata_graph, encoding, output_format):
        # self.ds = Dataset()
        # # self.ds = apply_default_namespaces(Dataset())
        # self.g = self.ds.graph(URIRef(identifier))

        self.identifier = identifier
        self.file_name = file_name
        self.metadata_uri = metadata_uri

        self.columns = columns
        self.schema = schema
        self.metadata_graph = metadata_graph
        self.encoding = encoding
        self.output_format = output_format

        self.templates = {}

        self.aboutURLSchema = self.schema.csvw_aboutUrl

    def equal_to_null(self, nulls, row):
        """Determines whether a value in a cell matches a 'null' value as specified in the CSVW schema)"""
        for n in nulls:
            n = Item(self.metadata_graph, n)
            col = str(n.csvw_name)
            val = str(n.csvw_null)
            if row[col] == val:
                # logger.debug("Value of column {} ('{}') is equal to specified 'null' value: '{}'".format(col, unicode(row[col]).encode('utf-8'), val))
                # There is a match with null value
                return True
        # There is no match with null value
        return False

    def process(self, count, rows, chunksize):
        """Process the rows fed to the converter. Count and chunksize are used to determine the
        current row number (needed for default observation identifiers)"""
        obs_count = count * chunksize

        # logger.info("Row: {}".format(obs_count)) #removed for readability

        nanopubs_string = bytes()

        # We iterate row by row, and then column by column, as given by the CSVW mapping file.
        mult_proc_counter = 0
        iter_error_counter= 0
        for row in rows:

            self.np = Nanopublication(self.file_name)
            # self.ds = apply_default_namespaces(Dataset())
            # self.ds = Dataset()
            # self.g = self.np.graph(URIRef(self.identifier + '/' + str(obs_count)))

            # This fixes issue:10
            if row is None:
                mult_proc_counter += 1
                # logger.debug( #removed for readability
                #     "Skipping empty row caused by multiprocessing (multiple of chunksize exceeds number of rows in file)...")
                continue

            # set the '_row' value in case we need to generate 'default' URIs for each observation ()
            # logger.debug("row: {}".format(obs_count)) #removed for readability
            row[u'_row'] = obs_count
            count += 1

            # print(row)

            # The self.columns dictionary gives the mapping definition per column in the 'columns'
            # array of the CSVW tableSchema definition.

            for c in self.columns:
                c = Item(self.metadata_graph, c)
                # default about URL
                s = self.expandURL(self.aboutURLSchema, row)

                try:
                    # Can also be used to prevent the triggering of virtual
                    # columns!

                    # Get the raw value from the cell in the CSV file
                    try:
                        # Python 2
                        value = row[unicode(c.csvw_name)]
                    except NameError:
                        # Python 3
                        value = row[str(c.csvw_name)]

                    # This checks whether we should continue parsing this cell, or skip it.
                    if self.isValueNull(value, c):
                        continue

                    # If the null values are specified in an array, we need to parse it as a collection (list)
                    elif isinstance(c.csvw_null, Item):
                        nulls = Collection(self.metadata_graph, BNode(c.csvw_null))

                        if self.equal_to_null(nulls, row):
                            # Continue to next column specification in this row, if the value is equal to (one of) the null values.
                            continue
                except:
                    # No column name specified (virtual) because there clearly was no c.csvw_name key in the row.
                    # logger.debug(traceback.format_exc()) #removed for readability
                    iter_error_counter +=1
                    if isinstance(c.csvw_null, Item):
                        nulls = Collection(self.metadata_graph, BNode(c.csvw_null))
                        if self.equal_to_null(nulls, row):
                            # Continue to next column specification in this row, if the value is equal to (one of) the null values.
                            continue

                try:
                    # This overrides the subject resource 's' that has been created earlier based on the
                    # schema wide aboutURLSchema specification.

                    try:
                        csvw_virtual = unicode(c.csvw_virtual)
                        csvw_name = unicode(c.csvw_name)
                        csvw_value = unicode(c.csvw_value)
                        about_url = unicode(c.csvw_aboutUrl)
                        value_url = unicode(c.csvw_valueUrl)
                    except NameError:
                        csvw_virtual = str(c.csvw_virtual)
                        csvw_name = str(c.csvw_name)
                        csvw_value = str(c.csvw_value)
                        about_url = str(c.csvw_aboutUrl)
                        value_url = str(c.csvw_valueUrl)

                    if csvw_virtual == u'true' and c.csvw_aboutUrl is not None:
                        s = self.expandURL(c.csvw_aboutUrl, row)

                    if c.csvw_valueUrl is not None:
                        # This is an object property, because the value needs to be cast to a URL
                        p = self.expandURL(c.csvw_propertyUrl, row)
                        o = self.expandURL(c.csvw_valueUrl, row)
                        if self.isValueNull(os.path.basename(unicode(o)), c):
                            logger.debug("skipping empty value")
                            continue

                        if csvw_virtual == u'true' and c.csvw_datatype is not None and URIRef(c.csvw_datatype) == XSD.anyURI:
                            # Special case: this is a virtual column with object values that are URIs
                            # For now using a test special property
                            value = row[unicode(c.csvw_name)].encode('utf-8')
                            o = URIRef(iribaker.to_iri(value))

                        if csvw_virtual == u'true' and c.csvw_datatype is not None and URIRef(c.csvw_datatype) == XSD.linkURI:
                            about_url = about_url[about_url.find("{"):about_url.find("}")+1]
                            s = self.expandURL(about_url, row)
                            # logger.debug("s: {}".format(s))
                            value_url = value_url[value_url.find("{"):value_url.find("}")+1]
                            o = self.expandURL(value_url, row)
                            # logger.debug("o: {}".format(o))

                        # For coded properties, the collectionUrl can be used to indicate that the
                        # value URL is a concept and a member of a SKOS Collection with that URL.
                        if c.csvw_collectionUrl is not None:
                            collection = self.expandURL(c.csvw_collectionUrl, row)
                            self.np.ag.add((collection, RDF.type, SKOS['Collection']))
                            self.np.ag.add((o, RDF.type, SKOS['Concept']))
                            self.np.ag.add((collection, SKOS['member'], o))

                        # For coded properties, the schemeUrl can be used to indicate that the
                        # value URL is a concept and a member of a SKOS Scheme with that URL.
                        if c.csvw_schemeUrl is not None:
                            scheme = self.expandURL(c.csvw_schemeUrl, row)
                            self.np.ag.add((scheme, RDF.type, SKOS['Scheme']))
                            self.np.ag.add((o, RDF.type, SKOS['Concept']))
                            self.np.ag.add((o, SKOS['inScheme'], scheme))
                    else:
                        # This is a datatype property
                        if c.csvw_value is not None:
                            value = self.render_pattern(csvw_value, row)
                        elif c.csvw_name is not None:
                            # print s
                            # print c.csvw_name, self.encoding
                            # print row[unicode(c.csvw_name)], type(row[unicode(c.csvw_name)])
                            # print row[unicode(c.csvw_name)].encode('utf-8')
                            # print '...'
                            value = row[csvw_name].encode('utf-8')
                        else:
                            raise Exception("No 'name' or 'csvw:value' attribute found for this column specification")

                        # If propertyUrl is specified, use it, otherwise use
                        # the column name
                        if c.csvw_propertyUrl is not None:
                            p = self.expandURL(c.csvw_propertyUrl, row)
                        else:
                            if "" in self.metadata_graph.namespaces():
                                propertyUrl = self.metadata_graph.namespaces()[""][
                                    csvw_name]
                            else:
                                propertyUrl = "{}{}".format(get_namespaces()['sdv'],
                                    csvw_name)

                            p = self.expandURL(propertyUrl, row)

                        if c.csvw_datatype is not None:
                            if URIRef(c.csvw_datatype) == XSD.anyURI:
                                # The xsd:anyURI datatype will be cast to a proper IRI resource.
                                o = URIRef(iribaker.to_iri(value))
                            elif URIRef(c.csvw_datatype) == XSD.string and c.csvw_lang is not None:
                                # If it is a string datatype that has a language, we turn it into a
                                # language tagged literal
                                # We also render the lang value in case it is a
                                # pattern.
                                o = Literal(value, lang=self.render_pattern(
                                    c.csvw_lang, row))
                            else:
                                try:
                                    csvw_datatype = unicode(c.csvw_datatype)
                                except NameError:
                                    csvw_datatype = str(c.csvw_datatype).split(')')[0].split('(')[-1]
                                # print(type(csvw_datatype))
                                # print(csvw_datatype)
                                o = Literal(value, datatype=csvw_datatype, normalize=False)
                        else:
                            # It's just a plain literal without datatype.
                            o = Literal(value)


                    # Add the triple to the assertion graph
                    self.np.ag.add((s, p, o))

                    # Add provenance relating the propertyUrl to the column id
                    if '@id' in c:
                        self.np.ag.add((p, PROV['wasDerivedFrom'], URIRef(c['@id'])))

                except:
                    # print row[0], value
                    traceback.print_exc()

            # We increment the observation (row number) with one
            obs_count += 1


            ### Provenance

            # # Add a prov:wasDerivedFrom between the nanopublication assertion graph
            # # and the metadata_uri
            self.np.pg.add((self.np.ag.identifier, PROV[
                           'wasDerivedFrom'], self.metadata_uri))
            # Add an attribution relation and dc:creator relation between the
            # # nanopublication, the assertion graph and the authors of the schema
            for o in self.metadata_graph.objects(self.metadata_uri, DC['creator']):
                self.np.pg.add((self.np.ag.identifier, PROV['wasAttributedTo'], o))
                self.np.add((self.np.uri, PROV['wasAttributedTo'], o))
                self.np.pig.add((self.np.ag.identifier, DC['creator'], o))


            # nanopubs_string += self.np.serialize(format=self.output_format)
            nanopubs_string += self.np.as_string(output_format=self.output_format)

        # for s,p,o in self.g.triples((None,None,None)):
        #     print(s.__repr__,p.__repr__,o.__repr__)

        logger.debug(
            "{} row skips caused by multiprocessing (multiple of chunksize exceeds number of rows in file)...".format(mult_proc_counter))
        logger.debug(
            "{} errors encountered while trying to iterate over a NoneType...".format(mult_proc_counter))
        logger.info("... done")
        # return self.ds.serialize(format=self.output_format)
        return nanopubs_string

    # def serialize(self):
    #     trig_file_name = self.file_name + '.trig'
    #     logger.info("Starting serialization to {}".format(trig_file_name))
    #
    #     with open(trig_file_name, 'w') as f:
    #         self.np.serialize(f, format='trig')
    #     logger.info("... done")

    def render_pattern(self, pattern, row):
        """Takes a Jinja or Python formatted string, and applies it to the row value"""
        # Significant speedup by not re-instantiating Jinja templates for every
        # row.
        if pattern in self.templates:
            template = self.templates[pattern]
        else:
            template = self.templates[pattern] = Template(pattern)

        # TODO This should take into account the special CSVW instructions such as {_row}
        # First we interpret the url_pattern as a Jinja2 template, and pass all
        # column/value pairs as arguments
        rendered_template = template.render(**row)

        try:
            # We then format the resulting string using the standard Python2
            # expressions
            return rendered_template.format(**row)
        except:
            logger.warning(
                u"Could not apply python string formatting, probably due to mismatched curly brackets. IRI will be '{}'. ".format(rendered_template))
            return rendered_template

    def expandURL(self, url_pattern, row, datatype=False):
        """Takes a Jinja or Python formatted string, applies it to the row values, and returns it as a URIRef"""

        try:
            unicode_url_pattern = unicode(url_pattern)
        except NameError:
            unicode_url_pattern = str(url_pattern).split(')')[0].split('(')[-1]
        # print(unicode_url_pattern)

        url = self.render_pattern(unicode_url_pattern, row)

        # DEPRECATED
        # for ns, nsuri in namespaces.items():
        #     if url.startswith(ns):
        #         url = url.replace(ns + ':', nsuri)
        #         break

        try:
            iri = iribaker.to_iri(url)
            rfc3987.parse(iri, rule='IRI')
        except:
            raise Exception(u"Cannot convert `{}` to valid IRI".format(url))

        # print(iri)
        return URIRef(iri)

    def isValueNull(self, value, c):
        """This checks whether we should continue parsing this cell, or skip it because it is empty or a null value."""
        try:
            if len(value) == 0 and unicode(c.csvw_parseOnEmpty) == u"true":
                print("Not skipping empty value")
                return False #because it should not be skipped
            elif len(value) == 0 or value == unicode(c.csvw_null) or value in [unicode(n) for n in c.csvw_null] or value == unicode(self.schema.csvw_null):
                # Skip value if length is zero and equal to (one of) the null value(s)
                logger.debug(
                    "Length is 0 or value is equal to specified 'null' value")
                return True
        except:
            logger.debug("null does not exist or is not a list.")
        return False
